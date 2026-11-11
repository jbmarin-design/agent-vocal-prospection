# Rappel sollicité : lancer un rappel à la main

Campagne `ehpad-rappel` : la personne a répondu à l'email 3 de la campagne Mautic EHPAD
(« Une démonstration un peu particulière ») avec un numéro et un moment. Sarah la rappelle,
présente OpteoLink, se donne en exemple d'assistant vocal pour un EHPAD, répond aux questions
et propose l'audit gratuit sur place.

La campagne reste `actif: false` : chaque rappel se lance **à la main**, au moment demandé.

## Ce qui change par rapport à la prospection

| | Prospection (`ehpad`) | Rappel sollicité (`ehpad-rappel`) |
|---|---|---|
| Ouverture | « …l'assistante virtuelle d'OpteoLink, les télécoms pro à Toulouse. Pourrais-je parler au directeur… ? » | « …l'assistante virtuelle d'OpteoLink, les télécoms pro à Toulouse. Je vous appelle suite à votre réponse à notre email. Je parle bien à Madame Marin ? » |
| Réplique quand la personne dit « c'est moi » | « …je vous appelle de la part de Jean-Baptiste Marin… Deux petites minutes ? » | « Merci ! Comme promis, je vous présente OpteoLink en quelques minutes… Et vous allez voir, je suis moi-même un exemple de ce qu'on peut faire. C'est un bon moment ? » |
| Standard, barrage | `prompts/accueil.md` | Aucun : `prompts/rappel.md` (si un collègue décroche, Sarah demande la personne par son nom, ou quand rappeler) |
| Annonce IA, enregistrement, opposition, durée max | identiques | identiques |

Réglage : `type_appel: rappel_sollicite` dans le YAML de la campagne (`src/avp/prompts.py`).

## Prérequis : une image qui connaît `type_appel`

Le champ est lu par le code de l'image (`agent-worker` et `orchestrator`). **Une image antérieure
à `be4e795` l'ignore sans erreur et appellerait en mode prospection** (« Pourrais-je parler… »).
Avant le premier vrai rappel, une fois la CI verte (onglet Actions) :

```bash
sudo -u avp git -C /opt/agent-vocal-prospection pull
cd /opt/agent-vocal-prospection/deploy
sudo -u avp docker compose pull agent-worker orchestrator
sudo -u avp docker compose up -d agent-worker orchestrator
avp prompt show ehpad-rappel | tail -3     # doit finir par « … suite à votre réponse à notre email. Je parle bien à … ? »
```

## Procédure (sur la VM, en root)

**1. Préparer le prospect** à partir de la réponse email : un CSV d'une ligne dans `data/rappels/`
(le dossier `data/` est vu par le conteneur sous `/app/data`).

- `contact` : **« Madame Marin »** ou « Monsieur Durand », en toutes lettres (c'est dit tel quel
  dans l'ouverture : pas « Mme ») ;
- `telephone` : le numéro donné dans la réponse ;
- `axonaut id` : l'identifiant de la société dans Axonaut (dans l'adresse de sa fiche). Sans lui,
  le post-appel n'écrit rien dans Axonaut ; avec lui, il met à jour l'opportunité de la société
  dans le pipeline « EHPAD 2026 » ;
- `notes` : le moment demandé et l'email de la personne. Un email présent dans les notes n'est pas
  redemandé, Sarah le fait seulement confirmer au moment du rendez-vous.

```bash
mkdir -p /opt/agent-vocal-prospection/data/rappels
cat > /opt/agent-vocal-prospection/data/rappels/tilleuls.csv <<'CSV'
nom;telephone;ville;contact;fonction;axonaut id;notes
EHPAD Les Tilleuls;06 12 34 56 78;Auch;Madame Marin;directrice;12345;Réponse à l'email 3 le 12/11 : rappeler mardi matin. Email : i.marin@laposte.net
CSV
chown -R avp: /opt/agent-vocal-prospection/data/rappels
avp campaign import ehpad-rappel --csv /app/data/rappels/tilleuls.csv --dry-run
avp campaign import ehpad-rappel --csv /app/data/rappels/tilleuls.csv
avp campaign stats ehpad-rappel          # « Prochains appels prévus » : noter l'ID du prospect
```

**2. Relire ce que Sarah dira** (facultatif) : `avp prompt show ehpad-rappel`.

**3. Au moment convenu, lancer l'appel** (ignore les plages horaires, respecte la liste
d'opposition ; le post-appel écrit dans Axonaut) :

```bash
avp call prospect <ID> --wait
```

**4. Après l'appel** : `avp calls list --last 3`, puis `avp calls show <call_id>` (transcription,
issue, score). Le RDV éventuel est créé dans l'agenda Axonaut avec email de confirmation.

Pour un essai sans écrire dans Axonaut, sur son propre portable :

```bash
avp call test --number "06 XX XX XX XX" --campaign ehpad-rappel --name "EHPAD test JB" --city Auch --contact "Monsieur Marin" --wait
```

## Simulations

`simulations/ehpad-rappel.yaml` (8 scénarios : curieuse de l'IA, pressé, prix, RDV demandé
d'emblée, refus poli, « vous êtes un robot ? », collègue qui décroche, pas le bon moment) :

```bash
sudo -u avp git -C /opt/agent-vocal-prospection pull
avp simulation ehpad-rappel --repetitions 2
```

Résultats consignés dans `docs/journal-tests.md`.
