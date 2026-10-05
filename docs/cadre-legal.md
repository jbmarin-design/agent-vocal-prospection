# Cadre légal — à faire valider par un juriste

> Ce document résume les règles connues à date (octobre 2026) et les choix de conception qui en découlent. **Ce n'est pas un avis juridique** : faire valider le dispositif (scripts, annonce, registre RGPD) par un juriste ou le DPO avant la mise en production.

## 1. Démarchage téléphonique : consommateurs vs professionnels

- Depuis le **11 août 2026** (décret n° 2026-662 du 23 juillet 2026, Code de la consommation), le démarchage téléphonique des **consommateurs** exige leur **consentement préalable** (opt-in). Bloctel ne suffit plus.
- Ce régime vise la personne **agissant comme consommateur**. La prospection **réellement B2B** (un établissement, appelé au sujet de son activité professionnelle) reste encadrée par le **RGPD** : base légale d'intérêt légitime, information, droit d'opposition (art. 21).
- **Conséquence pour le projet** : on n'appelle **que des numéros professionnels d'établissements** (standard d'EHPAD, cabinet, mairie), pour une offre en lien avec leur activité. **Jamais** de numéro personnel de particulier.

## 2. Plages horaires et fréquence

Les règles consommateurs (lun-ven 10h-13h et 14h-20h hors jours fériés, 4 sollicitations maximum par 30 jours) ne s'appliquent pas en principe au B2B. Le projet les adopte quand même **par prudence** :

- plages par défaut : 10h00-12h30 et 14h00-17h30, du lundi au vendredi ;
- `max_tentatives` ≤ 4 par campagne, avec un délai minimal entre deux tentatives ;
- pas d'appel les jours fériés (à gérer dans la configuration des plages ; voir `docs/exploitation.md`).

## 3. Intelligence artificielle (AI Act, art. 50)

Les obligations de transparence s'appliquent depuis le **2 août 2026** : une personne doit être **informée qu'elle interagit avec un système d'IA**, sauf si c'est évident.

**Implémentation** : la première phrase de l'agent annonce systématiquement qu'il s'agit d'un assistant vocal IA d'OpteoLink. Cette règle est codée dans `prompts/base.md` **et** dans le message d'ouverture du worker (non modifiable par la campagne). L'agent répond honnêtement « oui, je suis une IA » si on le lui demande, à tout moment.

## 4. Enregistrement et transcription

- L'interlocuteur est **informé** de l'enregistrement et de sa finalité (amélioration du service, suivi commercial) dans la phrase d'ouverture.
- S'il refuse l'enregistrement : l'agent le note, propose qu'un humain le rappelle et met fin à l'appel. L'outil `enregistrer_opposition` peut aussi être utilisé.
- **Durée de conservation proposée** : transcriptions et enregistrements conservés 6 mois, puis purgés (`avp purge`) ; l'analyse résumée reste dans Axonaut.

## 5. RGPD : à préparer

- [ ] Inscrire le traitement « prospection téléphonique assistée par IA » au **registre des traitements** d'OpteoLink.
- [ ] Rédiger une **mention d'information** accessible (page web) : identité du responsable, finalité, base légale (intérêt légitime), destinataires (sous-traitants : Deepgram, Anthropic, Cartesia/ElevenLabs, Axonaut), durée de conservation, droits, contact.
- [ ] Vérifier les **transferts hors UE** des sous-traitants IA (DPA, clauses contractuelles types) et privilégier les régions UE quand elles existent.
- [ ] Liste d'opposition : toute demande « ne plus m'appeler » est ajoutée **immédiatement et définitivement** (`optout`), vérifiée avant chaque appel.
- [ ] Analyse d'impact (AIPD) : à évaluer avec le DPO, du fait de l'usage d'une IA et de l'enregistrement de voix.

## 6. Règles codées dans l'agent (non désactivables)

1. Annonce de l'IA et de l'enregistrement dans la première phrase.
2. Pas de prix, pas d'engagement contractuel, pas de promesse technique ferme.
3. Respect immédiat d'un refus ou d'une demande d'opposition.
4. Jamais de mensonge sur l'identité ou sur l'objet de l'appel.
5. Durée maximale d'appel et raccrochage poli.
