# Google Agenda et emails de confirmation

L'agent utilise ton agenda Google (compte opteolink.fr) pour trois choses :

1. **Proposer uniquement des créneaux libres.** Avant chaque appel, le planificateur lit tes occupations (freebusy) sur les trois semaines à venir.
2. **Poser les RDV dans ton agenda.** Axonaut étant synchronisé avec Google Agenda, rien n'est écrit dans l'agenda Axonaut : le RDV y apparaît par la synchronisation.
3. **T'envoyer un email quand un RDV demande ta confirmation.**

## Règles de RDV (campagnes EHPAD et cabinets médicaux)

| Jour | Ce qui se passe |
|---|---|
| **Mardi, jeudi** | RDV direct : l'événement est créé dans ton agenda et **Google envoie l'invitation au client**. |
| **Lundi, vendredi** | Proposés seulement si aucun mardi ou jeudi ne convient. L'agent annonce au prospect que tu confirmeras par email. L'événement est créé avec le titre **« [À CONFIRMER] RDV OpteoLink — … »**, sans invité, et tu reçois un email. |
| Mardi ou jeudi **en conflit** (un événement ajouté entre-temps), ou prospect **sans email** | Même traitement que lundi et vendredi : à confirmer. |

**Pour confirmer** : dans Google Agenda (téléphone ou PC), retire « [À CONFIRMER] » du titre de l'événement. Dans la minute, l'orchestrateur ajoute le client en invité et Google lui envoie l'invitation.
**Pour refuser** : supprime l'événement, puis rappelle le prospect pour proposer un autre créneau.

Les jours se règlent dans chaque campagne (`campaigns/*.yaml`, section `rdv`) :

```yaml
rdv:
  plages:                  # RDV directs
    - jours: [2, 4]        # 1 = lundi … 7 = dimanche
      debut: "09:30"
      fin: "12:00"
    - jours: [2, 4]
      debut: "14:00"
      fin: "17:30"
  jours_a_confirmer: [1, 5]
  creneaux_a_confirmer: 2
```

## Mise en place (une seule fois, 15 minutes)

### 1. Projet Google Cloud

Avec ton compte opteolink.fr, sur [console.cloud.google.com](https://console.cloud.google.com) :

1. Crée un projet, par exemple `agent-vocal-opteolink`.
2. **API et services → Bibliothèque** : active **Google Calendar API** et **Gmail API**.
3. **API et services → Écran de consentement OAuth** :
   - type d'utilisateur **Interne** (réservé à ton domaine : ni validation Google, ni expiration du jeton au bout de 7 jours) ;
   - nom de l'application `Agent vocal OpteoLink`, email d'assistance `jbmarin@opteolink.fr`.
4. **API et services → Identifiants → Créer des identifiants → ID client OAuth** :
   - type d'application **Application de bureau** ;
   - copie l'**ID client** et le **code secret**.

### 2. Configuration sur la VM

Dans `.env` :

```bash
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
GOOGLE_CALENDAR_ID=primary            # ton agenda principal
RDV_CONFIRM_EMAIL=jbmarin@opteolink.fr
```

Puis : `cd /opt/agent-vocal-prospection/deploy && sudo -u avp docker compose up -d --force-recreate orchestrator`

### 3. Autorisation

L'autorisation se fait dans **ton navigateur**, mais la réponse de Google doit arriver sur la VM. Un tunnel SSH relie les deux.

1. **Sur ton PC**, ouvre un tunnel et laisse la fenêtre ouverte :
   ```bash
   ssh -L 8765:127.0.0.1:8765 <utilisateur>@10.64.0.11
   ```
2. **Sur la VM** (dans cette session SSH ou une autre) :
   ```bash
   avp google auth
   ```
3. Ouvre le lien affiché dans le navigateur de ton PC et connecte-toi avec jbmarin@opteolink.fr. Accepte trois droits : voir tes disponibilités, gérer les événements, envoyer des emails en ton nom.
4. La page affiche « Autorisation reçue » et la commande indique que le jeton est enregistré dans `data/google-token.json`.

### 4. Vérifications

```bash
avp google check        # nom de l'agenda + tes occupations des 7 prochains jours
avp google test-mail    # tu dois recevoir un email de test
avp check               # la ligne « Google Agenda » est OK
avp prompt show ehpad --role decideur   # créneaux directs puis « sous réserve »
```

## Ce que tu verras

- **Dans ton agenda** : « RDV OpteoLink — EHPAD Les Tilleuls ». La description contient l'interlocuteur, son email et son téléphone, le résumé de l'appel, le besoin, l'équipement en place et la référence de l'appel.
- **Dans ta boîte mail**, pour un RDV à confirmer : le sujet « RDV à confirmer : EHPAD …, vendredi 16 octobre à 10 h », les détails, le lien vers l'événement et la marche à suivre.
- **Dans Axonaut**, pour chaque appel :
  - l'événement « Appel IA », avec le résumé, les réponses, les objections et le RDV ;
  - l'opportunité, à la bonne étape et avec la bonne probabilité ;
  - les tâches de rappel ;
  - une tâche de **relance d'échéance** si un contrat ou une décision arrive à terme dans plusieurs mois. Par défaut, elle tombe 90 jours avant la date (`relance_avant_echeance_jours` dans la section `axonaut` de la campagne).

## Dépannage

| Symptôme | Cause probable |
|---|---|
| `avp google auth` : la page ne s'ouvre pas | Le tunnel SSH n'est pas ouvert, ou il pointe vers un autre port que `GOOGLE_OAUTH_PORT` (8765). |
| « access_denied » ou « app non vérifiée » | L'écran de consentement n'est pas en type **Interne**, ou le compte connecté n'est pas dans le domaine opteolink.fr. |
| « Google n'a pas renvoyé de refresh_token » | Relance `avp google auth`. La commande force déjà `prompt=consent`. |
| « rafraîchissement du jeton refusé » | Accès révoqué dans ton compte Google : relance `avp google auth`. |
| `403 insufficientPermissions` | Une API n'est pas activée dans le projet (Calendar ou Gmail). |
| Le RDV confirmé ne part pas | Le titre commence encore par « [À CONFIRMER] » (espace ou crochet oublié). Regarde aussi `docker compose logs orchestrator | grep Agenda`. |
