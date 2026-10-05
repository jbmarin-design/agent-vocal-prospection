"""Client asynchrone de l'API Axonaut v2 (https://axonaut.com/api/v2).

- Authentification par l'en-tête `userApiKey`.
- Réessais automatiques (429, 5xx, erreurs réseau) avec backoff exponentiel et respect de `Retry-After`.
- Erreurs 4xx → `AxonautError` portant le message renvoyé par l'API.
- Pagination tolérante : en-tête `page` **et** paramètre de requête `page`/`per_page` ;
  arrêt sur page vide, page déjà vue (pagination ignorée par l'API) ou page plus courte que les précédentes.
- Formes de réponse tolérées : liste brute, ou objet contenant la liste (`data`, `items`, `results`…).
- `dry_run` : aucune écriture (POST/PATCH) n'est envoyée ; un dict factice `{"dry_run": True, ...}` est
  retourné et l'écriture est journalisée. Les lectures restent réelles si une clé est fournie.

Les noms exacts de certains champs (téléphone, ville) ne sont pas garantis par la documentation
publique : `company_phone` et `company_city` cherchent dans plusieurs clés plausibles.
Voir `docs/axonaut.md` et la commande `avp axonaut check`.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import AsyncIterator, Iterable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import get_settings
from .phone import InvalidPhoneNumber, is_mobile_fr, normalize_phone

log = logging.getLogger("avp.axonaut")

TASK_PRIORITIES = ("basse", "normale", "haute", "urgente")
EVENT_NATURES = {1: "rendez-vous", 2: "email", 3: "appel", 4: "courrier", 5: "SMS", 6: "autre"}
_LIST_KEYS = ("data", "items", "results", "companies", "employees", "opportunities", "events", "tasks")
_MAX_PAGES = 500  # garde-fou contre une pagination qui ne s'arrêterait jamais


class AxonautError(RuntimeError):
    """Erreur renvoyée par l'API Axonaut (ou impossibilité de la joindre)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Utilitaires de forme de réponse
# ---------------------------------------------------------------------------


def _norm(s: Any) -> str:
    """Normalise une chaîne pour comparaison (casse, accents, espaces)."""
    txt = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(txt.casefold().split())


def _extract_list(payload: Any) -> list[dict]:
    """Extrait la liste d'objets d'une réponse (liste brute ou objet enveloppe)."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):  # ex. {"data": {"items": [...]}}
                inner = _extract_list(val)
                if inner:
                    return inner
        # Objet unique (ex. une seule société) : on ne le prend pas pour une liste
    return []


def _extract_object(payload: Any) -> dict:
    """Extrait l'objet d'une réponse (objet brut ou enveloppé dans `data`)."""
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict) and ("id" in inner or "name" in inner):
            return inner
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _item_key(item: dict) -> Any:
    return item.get("id") if item.get("id") is not None else repr(sorted(item.items(), key=lambda kv: kv[0]))


def _api_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500].strip() or resp.reason_phrase
    if isinstance(data, dict):
        for key in ("error", "message", "errors", "detail", "title"):
            if data.get(key):
                val = data[key]
                if isinstance(val, dict):
                    val = val.get("message") or val
                return str(val)[:500]
    return str(data)[:500]


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        return max(0.0, (dt - datetime.now(dt.tzinfo)).total_seconds())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Accesseurs tolérants sur les objets Axonaut
# ---------------------------------------------------------------------------


def opportunity_pipe(opp: dict) -> str:
    pipe = opp.get("pipe_name") or opp.get("pipe")
    if isinstance(pipe, dict):
        pipe = pipe.get("name")
    return str(pipe or "")


def opportunity_step(opp: dict) -> str:
    step = opp.get("pipe_step_name") or opp.get("pipe_step") or opp.get("step")
    if isinstance(step, dict):
        step = step.get("name")
    return str(step or "")


def opportunity_company_id(opp: dict) -> int | None:
    comp = opp.get("company")
    cid = comp.get("id") if isinstance(comp, dict) else opp.get("company_id")
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None


_COMPANY_PHONE_KEYS = (
    "phone_number", "phone", "telephone", "tel", "standard", "switchboard", "landline", "fixed_phone",
    "office_phone", "company_phone",
)
_COMPANY_MOBILE_KEYS = ("cellphone_number", "cellphone", "mobile", "mobile_phone")
_EMPLOYEE_PHONE_KEYS = ("phone_number", "phone", "telephone", "tel", "office_phone", "landline")
_EMPLOYEE_MOBILE_KEYS = ("cellphone_number", "cellphone", "mobile", "mobile_phone")


def _phones_from(obj: dict, keys: Iterable[str]) -> list[str]:
    out: list[str] = []
    for k in keys:
        val = obj.get(k)
        if isinstance(val, str) and val.strip():
            try:
                out.append(normalize_phone(val))
            except InvalidPhoneNumber:
                log.debug("numéro ignoré (%s=%r)", k, val)
    return out


def _custom_field_phones(obj: dict) -> list[str]:
    cf = obj.get("custom_fields")
    if not isinstance(cf, dict):
        return []
    keys = [k for k in cf if any(w in _norm(k) for w in ("tel", "phone", "standard"))]
    return _phones_from(cf, keys)


def company_phone(company: dict, employees: list[dict] | None = None) -> str | None:
    """Meilleur numéro professionnel E.164 d'une société Axonaut.

    Ordre : fixe de la société (standard, adresses, champs personnalisés) → fixe d'un contact
    (contact de facturation en premier) → mobile de la société → mobile d'un contact.
    Un mobile n'est jamais retenu si un fixe existe. Retourne None si rien d'exploitable.
    """
    company = company or {}
    company_nums: list[str] = _phones_from(company, _COMPANY_PHONE_KEYS)
    for addr in company.get("addresses") or []:
        if isinstance(addr, dict):
            company_nums += _phones_from(addr, _COMPANY_PHONE_KEYS)
    company_nums += _custom_field_phones(company)
    company_mobiles = _phones_from(company, _COMPANY_MOBILE_KEYS)

    emps = employees if employees is not None else [e for e in company.get("employees") or [] if isinstance(e, dict)]
    emps = sorted(emps, key=lambda e: not bool(e.get("is_billing_contact")))
    emp_nums: list[str] = []
    emp_mobiles: list[str] = []
    for e in emps:
        emp_nums += _phones_from(e, _EMPLOYEE_PHONE_KEYS)
        emp_mobiles += _phones_from(e, _EMPLOYEE_MOBILE_KEYS)

    for group in (company_nums, emp_nums):
        fixed = [n for n in group if not is_mobile_fr(n)]
        if fixed:
            return fixed[0]
    # Aucun fixe : un numéro « fixe » qui s'avère mobile, puis les champs mobiles
    for group in (company_nums, company_mobiles, emp_nums, emp_mobiles):
        if group:
            return group[0]
    return None


def company_city(company: dict) -> str:
    """Ville de la société (plusieurs formes de réponse tolérées) ; chaîne vide si inconnue."""
    company = company or {}
    for key in ("address_city", "city", "ville", "town"):
        val = company.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("address", "billing_address", "main_address", "delivery_address"):
        val = company.get(key)
        if isinstance(val, dict):
            city = company_city(val)
            if city:
                return city
    for addr in company.get("addresses") or []:
        if isinstance(addr, dict):
            city = company_city(addr)
            if city:
                return city
    return ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AxonautClient:
    """Client asynchrone Axonaut v2. À utiliser de préférence en `async with`."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        dry_run: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        user_email: str | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        max_backoff: float = 60.0,
        timezone: str | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.axonaut_api_key
        self.base_url = (base_url or settings.axonaut_base_url).rstrip("/")
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.user_email = user_email if user_email is not None else settings.axonaut_user_email
        self.max_retries = max(0, max_retries)
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.tz = ZoneInfo(timezone or settings.timezone)
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["userApiKey"] = self.api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> AxonautClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- bas niveau -----------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:  # surchargé dans les tests
        await asyncio.sleep(seconds)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if not self.api_key:
            raise AxonautError("clé API Axonaut absente (AXONAUT_API_KEY dans .env)")
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if attempt > self.max_retries:
                    raise AxonautError(f"Axonaut injoignable ({method} {path}) : {exc!r}") from exc
                delay = min(self.max_backoff, self.backoff_base * 2 ** (attempt - 1))
                log.warning("Axonaut %s %s : erreur réseau %r, nouvel essai dans %.1fs", method, path, exc, delay)
                await self._sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.max_retries:
                    raise AxonautError(
                        f"Axonaut {resp.status_code} sur {method} {path} après {attempt} essais : {_api_message(resp)}",
                        resp.status_code,
                    )
                delay = _retry_after_seconds(resp)
                if delay is None:
                    delay = self.backoff_base * 2 ** (attempt - 1)
                delay = min(self.max_backoff, delay)
                log.warning("Axonaut %s %s : HTTP %s, nouvel essai dans %.1fs", method, path, resp.status_code, delay)
                await self._sleep(delay)
                continue

            if resp.status_code >= 400:
                raise AxonautError(
                    f"Axonaut {resp.status_code} sur {method} {path} : {_api_message(resp)}", resp.status_code
                )
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise AxonautError(f"réponse Axonaut non JSON sur {method} {path} : {resp.text[:200]!r}") from exc

    async def _write(self, method: str, path: str, payload: dict[str, Any]) -> dict:
        payload = {k: v for k, v in payload.items() if v is not None}
        if self.dry_run:
            log.info("[dry-run] Axonaut %s %s %s", method, path, payload)
            return {"dry_run": True, "id": None, "method": method, "path": path, "payload": payload}
        return _extract_object(await self._request(method, path, json=payload))

    async def _iter_pages(
        self, path: str, params: dict[str, Any] | None = None, per_page: int = 100
    ) -> AsyncIterator[list[dict]]:
        """Itère sur les pages brutes d'une liste. Voir la docstring du module pour les règles d'arrêt."""
        seen: set[Any] = set()
        prev_len: int | None = None
        for page in range(1, _MAX_PAGES + 1):
            q = dict(params or {})
            q.update({"page": page, "per_page": per_page})
            items = _extract_list(await self._request("GET", path, params=q, headers={"page": str(page)}))
            if not items:
                return
            keys = [_item_key(it) for it in items]
            if all(k in seen for k in keys):
                return  # l'API ignore la pagination et renvoie toujours la même page
            seen.update(keys)
            yield items
            # Page plus courte que demandé : dernière page si elle est aussi plus courte que la précédente
            # (si l'API impose sa propre taille de page, la 1re page peut être < per_page sans être la dernière).
            if len(items) < per_page and prev_len is not None and len(items) < prev_len:
                return
            prev_len = len(items)
        log.warning("Axonaut %s : arrêt de la pagination après %d pages (garde-fou)", path, _MAX_PAGES)

    async def _read_list(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        if not self.api_key and self.dry_run:
            log.info("[dry-run] lecture Axonaut %s ignorée (pas de clé)", path)
            return []
        out: list[dict] = []
        async for items in self._iter_pages(path, params):
            out.extend(items)
        return out

    # -- lectures ------------------------------------------------------------

    async def search_companies(self, query: str) -> list[dict]:
        """Sociétés dont le nom ou le code correspond à `query`."""
        return await self._read_list("/companies", {"search": query})

    async def get_company(self, company_id: int) -> dict:
        if not self.api_key and self.dry_run:
            return {}
        return _extract_object(await self._request("GET", f"/companies/{int(company_id)}"))

    async def list_company_employees(self, company_id: int) -> list[dict]:
        return await self._read_list(f"/companies/{int(company_id)}/employees")

    async def get_opportunity(self, opportunity_id: int) -> dict:
        if not self.api_key and self.dry_run:
            return {}
        return _extract_object(await self._request("GET", f"/opportunities/{int(opportunity_id)}"))

    async def list_opportunities(
        self, *, pipe_name: str | None = None, page: int = 1, per_page: int = 100
    ) -> list[dict]:
        """Une page d'opportunités (filtrée sur le pipeline côté client si l'API ignore le filtre)."""
        if not self.api_key and self.dry_run:
            return []
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if pipe_name:
            params["pipe_name"] = pipe_name
        items = _extract_list(await self._request("GET", "/opportunities", params=params, headers={"page": str(page)}))
        if pipe_name:
            items = [o for o in items if _norm(opportunity_pipe(o)) == _norm(pipe_name)]
        return items

    async def iter_opportunities(
        self, *, pipe_name: str | None = None, step_name: str | None = None
    ) -> AsyncIterator[dict]:
        """Toutes les opportunités, filtrées (côté client) par pipeline et étape."""
        if not self.api_key and self.dry_run:
            return
        params = {"pipe_name": pipe_name} if pipe_name else None
        async for items in self._iter_pages("/opportunities", params):
            for opp in items:
                if pipe_name and _norm(opportunity_pipe(opp)) != _norm(pipe_name):
                    continue
                if step_name and _norm(opportunity_step(opp)) != _norm(step_name):
                    continue
                yield opp

    async def find_opportunity(self, company_id: int, pipe_name: str) -> dict | None:
        """Opportunité non archivée de la société dans ce pipeline (la plus récente), ou None."""
        found: list[dict] = []
        async for opp in self.iter_opportunities(pipe_name=pipe_name):
            if opportunity_company_id(opp) == int(company_id) and not opp.get("is_archived"):
                found.append(opp)
        if not found:
            return None
        return max(found, key=lambda o: int(o.get("id") or 0))

    # -- écritures -----------------------------------------------------------

    def _iso(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz).isoformat(timespec="seconds")

    async def create_event(
        self,
        *,
        company_id: int,
        title: str,
        content: str,
        date: datetime,
        nature: int = 3,
        duration_min: int | None = None,
        opportunity_id: int | None = None,
        is_done: bool = True,
    ) -> dict:
        """Événement de timeline (nature : 1=RDV, 2=Email, 3=Appel, 4=Courrier, 5=SMS, 6=Autre)."""
        if nature not in EVENT_NATURES:
            raise ValueError(f"nature d'événement invalide : {nature}")
        payload: dict[str, Any] = {
            "company_id": int(company_id),
            "nature": nature,
            "title": title[:255],
            "content": content,
            "date": self._iso(date),
            "duration": int(duration_min) if duration_min is not None else None,
            "opportunity_id": int(opportunity_id) if opportunity_id else None,
            "is_done": bool(is_done),
            "employee_email": self.user_email or None,
        }
        return await self._write("POST", "/events", payload)

    async def create_opportunity(
        self,
        *,
        company_id: int,
        pipe_name: str,
        step_name: str,
        name: str = "",
        amount: float | None = None,
        probability: float | None = None,
        comments: str = "",
    ) -> dict:
        if not self.user_email:
            raise AxonautError("AXONAUT_USER_EMAIL requis pour créer une opportunité (business_manager_email)")
        payload: dict[str, Any] = {
            "company_id": int(company_id),
            "pipe_name": pipe_name,
            "pipe_step_name": step_name,
            "business_manager_email": self.user_email,
            "name": name or None,
            "amount": amount,
            "probability": max(0.0, min(100.0, float(probability))) if probability is not None else None,
            "comments": comments or None,
        }
        return await self._write("POST", "/opportunities", payload)

    async def update_opportunity(
        self,
        opportunity_id: int,
        *,
        step_name: str | None = None,
        probability: float | None = None,
        comments: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "pipe_step_name": step_name,
            "probability": max(0.0, min(100.0, float(probability))) if probability is not None else None,
            "comments": comments,
        }
        return await self._write("PATCH", f"/opportunities/{int(opportunity_id)}", payload)

    async def create_task(
        self,
        *,
        title: str,
        company_id: int | None = None,
        description: str = "",
        due: date | None = None,
        priority: str = "normale",
    ) -> dict:
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"priorité invalide : {priority!r} (attendu : {', '.join(TASK_PRIORITIES)})")
        if isinstance(due, datetime):
            due = (due if due.tzinfo is None else due.astimezone(self.tz)).date()
        today = datetime.now(self.tz).date()
        payload: dict[str, Any] = {
            "title": title[:255],
            "company_id": int(company_id) if company_id else None,
            "description": description or None,
            "priority": priority,
            "start_date": min(today, due).strftime("%d/%m/%Y") if due else None,
            "end_date": due.strftime("%d/%m/%Y") if due else None,
        }
        return await self._write("POST", "/tasks", payload)
