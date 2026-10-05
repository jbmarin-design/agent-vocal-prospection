# Prompts et campagnes : comment les modifier

L'essentiel du comportement de l'agent ne se trouve pas dans le code Python : il est dans **des fichiers texte que vous pouvez éditer**.

| Fichier | Ce qu'il règle | Quand le modifier |
|---|---|---|
| `prompts/base.md` | Identité, façon de parler, règles non négociables | Rarement (ton général, nouvelle règle) |
| `prompts/accueil.md` | Comment passer le standard | Si l'agent échoue souvent au standard |
| `prompts/decideur.md` | Déroulé avec le décideur, choix de l'issue | Si la qualification ou la prise de RDV accroche |
| `prompts/repondeur.md` | Message laissé sur messagerie | Si vous activez les messages |
| `prompts/analyse_post_appel.md` | Comment Sonnet note l'appel après coup | Si le scoring ne vous paraît pas juste |
| `campaigns/<id>.yaml` | Offre, accroche, cible, questions, objections, scoring, plages, Axonaut | **Le plus souvent** |
| `src/avp/prompts.py` → `OPENING_*` | Phrase d'ouverture (annonce IA + enregistrement) | Avec prudence : obligation légale |
| `src/avp/agent/agents.py` | Docstrings des outils (lues par Claude) | Si un outil est mal utilisé |

## Comment les couches s'assemblent

À chaque appel, `build_instructions()` concatène, séparés par `---` :

1. `base.md`
2. `accueil.md` **ou** `decideur.md`
3. la section campagne (générée depuis le YAML)
4. la fiche prospect (nom, ville, contact, notes Axonaut)
5. le contexte (date, heure, tentative, créneaux de RDV)

Pour voir le résultat exact : `avp prompt show ehpad --role decideur`.

Variables utilisables dans les `.md` : `{{entreprise}}`, `{{humain}}`, `{{humain_prenom}}`, `{{campagne}}`, `{{objectif}}`, `{{interlocuteur_cible}}`, `{{alternatives}}`, `{{prospect_nom}}`, `{{prospect_ville}}`, `{{contact_nom}}`, `{{contact_fonction}}`, `{{date_heure}}`, `{{tentative}}`, `{{duree_rdv}}`, `{{mode_rdv}}`, `{{phrase_ouverture}}`.

## Écrire pour la voix

- Phrases de 20 mots maximum, une idée par phrase, une question à la fois.
- Pas de listes dans ce que l'agent doit *dire* (les listes dans les consignes, oui).
- Écrire les nombres comme on les prononce dans les réponses types (« dix-huit ans »).
- Tester chaque modification en console (`docs/protocoles/phase-2.md` § 2.3) avant de la mettre en production.

## Créer une campagne

1. Copier `campaigns/ehpad.yaml` en `campaigns/<nouvel-id>.yaml` (id : minuscules, chiffres, tirets ; identique au nom du fichier).
2. Adapter `nom`, `cible`, `offre`, `accroche`, `qualification` (chaque question a un `id` unique en `snake_case`), `objections`, `scoring`, `plages`, `axonaut.pipe` et `axonaut.etapes`.
3. Vérifier : `avp campaign show <id>` puis `avp prompt show <id> --role decideur`.
4. Créer le pipeline et ses étapes dans Axonaut avec **exactement** les mêmes noms (`docs/axonaut.md`).
5. Tester en console puis par `avp call test` sur votre portable.

Les dossiers `campaigns/` et `prompts/` sont montés en lecture seule dans les conteneurs : une modification est prise en compte **au prochain appel**, sans reconstruire l'image.

## Versions de prompts et amélioration continue

Chaque appel enregistre une **version de prompt** : une empreinte des fichiers `prompts/*.md`, de la phrase d'ouverture et du YAML de la campagne. Le rapport hebdomadaire (`avp report weekly`) compare les taux de joignabilité, de RDV et la qualité d'appel **par version**. Vous savez ainsi si une modification a amélioré ou dégradé les résultats.

Bonne pratique : une modification à la fois, un commit Git par modification, avec un message qui dit ce qu'on cherche à améliorer (« accueil : motif plus court pour réduire les barrages »).
