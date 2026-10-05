# Axonaut : prérequis et intégration

L'agent lit Axonaut pour importer les prospects (phase 3), puis y écrit le résultat de chaque appel (phase 4). Le code est dans `src/avp/axonaut.py` (client API v2) et `src/avp/postcall.py` (synchronisation après l'appel).

## 1. Clé API

1. Dans Axonaut, ouvrir **Paramètres → API** (menu du compte) et copier la **clé API utilisateur**.
2. Dans `.env` :
   ```
   AXONAUT_API_KEY=<la clé>
   AXONAUT_BASE_URL=https://axonaut.com/api/v2
   AXONAUT_USER_EMAIL=jbmarin@opteolink.fr
   ```
   `AXONAUT_USER_EMAIL` doit être l'email d'un **utilisateur Axonaut existant**. Il sert de responsable (`business_manager_email`) des opportunités créées et d'auteur (`employee_email`) des événements.
3. La clé donne les droits de l'utilisateur qui l'a générée : ne la commitez jamais et ne la partagez pas.

Pour un essai sans aucune écriture, mettez `DRY_RUN=true` : les lectures restent réelles (si la clé est fournie), et les écritures (POST/PATCH) sont seulement **journalisées** (`[dry-run] Axonaut POST /events {...}`).

## 2. Pipelines et étapes à créer pour chaque campagne

Chaque campagne (`campaigns/<id>.yaml`) indique son pipeline et ses étapes dans le bloc `axonaut`. **Les noms doivent correspondre exactement** (casse et accents compris) aux pipelines et étapes qui existent dans Axonaut (**CRM → Opportunités → Paramètres des pipelines**). Si une étape n'existe pas, l'API renvoie une erreur 4xx, qui est consignée dans `calls.error`.

```yaml
axonaut:
  pipe: "Cabinets médicaux 2026"      # pipeline existant
  etape_initiale: "À contacter"        # étape utilisée si on crée une opportunité sans étape mappée
  montant_defaut: 900                  # montant des opportunités créées (facultatif)
  etapes:                              # clé = issue d'appel OU score → nom d'étape Axonaut
    rdv: "RDV planifié"
    transfert: "RDV planifié"
    rappel: "À rappeler"
    qualifie: "Qualifié"
    refus: "Perdu"
    non_qualifie: "Hors cible"
    barrage: "À contacter"
    chaud: "Qualifié"                  # utilisé si l'issue n'est pas mappée
    tiede: "Intéressé"
    froid: "À contacter"
```

**Priorité** (`target_step`) : on cherche d'abord l'**issue** de l'appel (`rdv`, `rappel`, `refus`, `qualifie`…, voir `CallOutcome` dans `src/avp/models.py`), puis le **score** (`chaud`, `tiede`, `froid`). Si rien n'est mappé, l'étape de l'opportunité n'est pas modifiée.

Étapes à créer, à adapter à chaque pipeline (EHPAD, cabinets médicaux, mairies…) :

| Étape suggérée | Utilisée pour |
|---|---|
| À contacter | étape initiale (déjà présente dans « Cabinets médicaux 2026 ») |
| À rappeler | rappel convenu |
| Intéressé | qualifié tiède |
| Qualifié | qualifié chaud |
| RDV planifié | RDV confirmé ou transfert à JB |
| Perdu / Hors cible | refus, hors cible |

Les campagnes **sans bloc `axonaut`** (par exemple la campagne de test `echo`) n'écrivent rien.

## 3. Ce que le post-appel écrit dans Axonaut

Rien n'est écrit si : l'appel est en `test_mode`, la campagne n'a pas de bloc `axonaut`, le prospect n'a pas d'`axonaut_company_id`, ou personne n'a été joint (non décroché, occupé, répondeur, SVI, erreur).

Sinon, pour la **société** du prospect :

| Objet | Quand | Contenu |
|---|---|---|
| **Opportunité** (PATCH) | une opportunité existe déjà (id connu du prospect, ou trouvée dans le pipeline de la campagne pour cette société) | `pipe_step_name` = étape cible, `probability` = `score_num` (0-100), `comments` = une ligne datée **ajoutée en tête** de l'historique existant |
| **Opportunité** (POST) | aucune opportunité, et l'issue n'est ni `opposition` ni `mauvais_numero` | pipeline de la campagne, étape cible (sinon `etape_initiale`), nom « Établissement — Campagne », `amount` = `montant_defaut`, probabilité, commentaire |
| **Événement appel** (`nature=3`) | toujours | titre `Appel IA — <issue> — <score> (<n>/100)` ; contenu : résumé, interlocuteur, besoin, équipement et prestataire actuels, réponses de qualification, objections, points clés, prochaine action, date de relance, RDV ou rappel, chemin de la transcription locale ; durée en minutes ; date = début de l'appel ; rattaché à l'opportunité |
| **Événement RDV** (`nature=1`, `is_done=false`) | RDV confirmé | à la date et pour la durée du RDV, avec la personne, l'email, le mode et les notes |
| **Tâche** « Préparer RDV <établissement> » | RDV confirmé | échéance la veille du RDV, priorité haute |
| **Tâche** « Rappeler <personne> — <établissement> » | rappel convenu | échéance à la date du rappel (le planificateur rappelle aussi automatiquement) |
| **Tâche** « Relancer <établissement> » | qualifié ou transfert avec une date de relance | échéance à la date de relance |
| **Événement** « Opposition au démarchage » (`nature=6`) | opposition | aucune opportunité ni tâche n'est créée |

> La synchronisation n'est **pas rejouée automatiquement** (pour éviter les doublons d'événements). En cas d'échec partiel, `calls.axonaut_synced = 0` et `calls.error` décrit ce qui a échoué ; corrigez la cause (étape manquante, clé…) et complétez Axonaut à la main si besoin.

## 4. Robustesse du client

- Authentification par l'en-tête `userApiKey`, réponses en `Accept: application/json`.
- Jusqu'à 3 nouveaux essais sur les réponses 429 et 5xx et sur les erreurs réseau, avec un backoff exponentiel (1 s, 2 s, 4 s) ou le délai indiqué par `Retry-After`.
- Une réponse 4xx lève `AxonautError` avec le message de l'API.
- La pagination envoie à la fois l'en-tête `page` et les paramètres de requête `page`/`per_page`. Elle s'arrête sur une page vide, sur une page identique à une page déjà reçue, ou sur une page plus courte que la précédente.
- Les filtres `pipe_name` et l'étape sont **aussi** appliqués côté client, sans tenir compte de la casse ni des accents.

## 5. À vérifier à la première exécution : `avp axonaut check`

La documentation publique d'Axonaut ne garantit pas le nom de tous les champs. Le code cherche donc dans plusieurs clés plausibles. Lors d'un test réel en lecture (octobre 2026), l'API a renvoyé :

- société : `address_city`, `address_zip_code`, `address_street`, `business_manager.email`, `employees[]`, `custom_fields` ; **aucun champ téléphone au niveau de la société** ;
- contact (`/companies/{id}/employees`) : `phone_number`, `cellphone_number`, `is_billing_contact`, `firstname`, `lastname`, `job` ;
- opportunité : `pipe_name`, `pipe_step_name`, `company.id`, `comments`, `probability`, `is_archived`.

`company_phone()` cherche le standard de la société (`phone_number`, `phone`, `telephone`, `addresses[].phone…`, ou un champ personnalisé dont le nom contient « tél », « phone » ou « standard »). À défaut, il prend le **fixe d'un contact**, le contact de facturation en premier. Un mobile n'est retenu que si aucun fixe n'existe. `company_city()` lit `address_city`, `city`, `address.city`, puis `addresses[].city`.

`avp axonaut check` (exposée par la CLI) appelle `search_companies`, `get_company`, `list_company_employees` et `list_opportunities`, puis affiche ce que `company_phone` et `company_city` en extraient. À vérifier :

1. **La clé fonctionne** : pas d'erreur 401 ou 403.
2. **Téléphone** : pour 5 sociétés connues, le numéro extrait est bien le **standard** de l'établissement, au format E.164. Si le standard est stocké dans un champ personnalisé, nommez-le « Téléphone standard ». S'il est stocké sous une autre clé, ajoutez-la à `_COMPANY_PHONE_KEYS` dans `src/avp/axonaut.py`.
3. **Ville** : elle est remplie pour ces sociétés.
4. **Pipelines** : `list_opportunities(pipe_name="…")` renvoie bien les opportunités du pipeline de la campagne, et les noms d'étapes affichés correspondent à ceux du YAML.
5. **Écriture (une seule fois, sur une société de test)** : lancez un appel de test avec `test_mode=false` vers une fiche « TEST OpteoLink », puis vérifiez dans Axonaut l'événement « Appel IA », l'étape et la probabilité de l'opportunité, et la tâche. Vérifiez aussi que `is_done` est respecté (un RDV doit apparaître « à faire »). Si Axonaut refuse le booléen, signalez-le : le connecteur MCP envoie `"true"`/`"false"` sous forme de chaîne.
