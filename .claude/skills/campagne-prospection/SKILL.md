---
name: campagne-prospection
description: Créer, calibrer ou retoucher une campagne de l'agent vocal de prospection OpteoLink (Sarah) pour une nouvelle cible métier — fichier campaigns/<id>.yaml, scénarios simulations/<id>.yaml, validation par simulation et mise en service. À utiliser dès que JB parle de nouvelle campagne, nouvelle cible, nouveau secteur à prospecter, accroche, objections, qualification ou pipeline Axonaut d'une campagne.
---

# Campagne de prospection : méthode

1. **Lire d'abord** `docs/campagnes/guide-creation.md` (fonctionnement de l'agent, champs, méthode
   de calibrage, limites des prompts communs, validation), puis `campaigns/ehpad.yaml` et
   `simulations/ehpad.yaml` (référence calibrée et validée au téléphone).
2. **Brief** : poser à JB, **en une seule fois**, les questions de `docs/campagnes/fiche-brief.md`
   auxquelles sa demande ne répond pas encore. Il dicte : accepter des réponses en vrac. Ne rien
   inventer (preuve, chiffre, nom de client, étape Axonaut) : marquer « à confirmer ».
3. **Adapter les tournures propres à l'offre** (guide § 2) : par défaut, les prompts communs
   parlent de l'audit télécom gratuit sur place des EHPAD. Si l'offre, le RDV (visio…) ou la cible
   diffèrent, renseigner `motif_appel`, `proposition` (`nom`, `exemple`, `detail`) et au besoin
   `presentation_entreprise`, plutôt que de contredire le prompt commun dans
   `instructions_specifiques`. Signaler à JB ce qui reste commun (`base.md`, motif au standard).
4. **Écrire** `campaigns/<id>.yaml` depuis `docs/campagnes/modele-campagne.yaml` (`actif: false`)
   et `simulations/<id>.yaml` depuis `docs/campagnes/modele-simulations.yaml` (10 à 20 scénarios).
   Tout ce que Sarah dira s'écrit à l'oral (30 mots, vouvoiement, ton enjoué, nombres en lettres).
5. **Contrôler** en local : `pytest -q && ruff check src tests`, `avp campaign show <id>`,
   `avp prompt show <id>` (relire le prompt assemblé), puis la liste de contrôle du guide § 7.
6. **Livrer** : commit en français (ligne Co-Authored-By habituelle), push sur `main`. Sur la VM
   (`ssh root@10.64.0.11`, accès autorisé par JB ; ne jamais toucher à son tmux) :
   `sudo -u avp git -C /opt/agent-vocal-prospection pull` puis
   `avp simulation <id> --repetitions 2`. Résumer à JB les écarts du rapport, corriger par lot.
7. **Ce qui reste à JB** : créer le pipeline Axonaut et ses étapes (noms exacts), valider
   l'appel de test au téléphone, décider de l'import et de l'activation (`actif: true`).

Règles intangibles : ne jamais retirer ni affaiblir l'annonce IA, l'enregistrement, l'opposition,
les plages ni la durée maximale ; jamais de prix ni de promesse chiffrée ; jamais afficher les
clés du `.env` de la VM. Une campagne ne doit pas contredire `prompts/base.md`.
