# Fiche brief d'une nouvelle campagne

À remplir avec JB **avant** d'écrire `campaigns/<id>.yaml`. Claude pose toutes les questions en
une fois (JB peut répondre en vrac, à l'oral) ; ce qui reste sans réponse est signalé comme
« à confirmer », jamais inventé. Chaque réponse alimente un champ de la campagne (entre crochets).

## 1. La cible

1. Quel type d'établissement ou d'entreprise ? Taille, statut (public, privé, groupe) ? Zone
   géographique ? [`nom`, `instructions_specifiques`]
2. Qui décide des télécoms chez eux ? Qui d'autre peut orienter la décision ? [`cible`]
3. Qui décroche au standard : accueil, secrétariat, standard mutualisé, serveur vocal ? Est-ce
   un barrage fréquent ? [`instructions_specifiques`]
4. Quand sont-ils joignables, quand faut-il éviter d'appeler (rush, réunions, saisons) ?
   [`plages`]
5. D'où viennent les numéros : pipeline Axonaut existant (nom et étapes), fichier CSV ?
   [`axonaut`, import]

## 2. Le métier et ses problèmes

6. Comment se passe leur quotidien côté télécoms ? Qu'est-ce qui les gêne, les inquiète, leur
   coûte ? Citez des situations vécues chez vos clients. [`offre.resume`, `accroche`]
7. Quels problèmes savez-vous résoudre pour eux, et qu'est-ce que ça change concrètement ?
   [`offre.points_forts`]
8. Quelles contraintes propres au secteur : réglementation, sécurité, urgences, secret,
   astreintes, équipements spécifiques ? [`instructions_specifiques`, `mots_cles_stt`]
9. Quels opérateurs, matériels, logiciels ou prestataires rencontrez-vous chez eux ?
   [`qualification`, `mots_cles_stt`]

## 3. L'offre et l'objectif

10. Que propose-t-on au téléphone : audit, diagnostic, démonstration, devis, visite ? Gratuit,
    sans engagement ? [`offre`, `objectif`]
11. Quel rendez-vous : sur place, visio, téléphone ? Durée ? Quels jours et heures pour vous,
    lesquels sous réserve ? Délai minimal et horizon ? [`rdv`]
12. Si ce n'est ni un audit ni une visite sur place : le dire, car les prompts communs supposent
    aujourd'hui « audit télécom, sur place » (voir le guide, § 2).

## 4. Les arguments et les preuves

13. Pourquoi OpteoLink plutôt qu'un autre pour cette cible ? [`points_forts`]
14. Quelles preuves peut-on citer **telles quelles** : clients du secteur (sans nom ou avec
    accord), retours, années d'expérience ? Aucun chiffre ni nom sans votre accord. [`preuves`]
15. Qu'est-ce qu'il ne faut surtout pas dire à cette cible ? [`instructions_specifiques`]

## 5. La conversation

16. Qu'avez-vous besoin de savoir avant le rendez-vous ? Lesquelles de ces informations sont
    prioritaires ? [`qualification`]
17. Quelles objections entendez-vous sur le terrain, et que répondez-vous ? [`objections`]
18. À quoi reconnaît-on un prospect chaud, tiède, froid ? [`scoring`]
19. Messagerie : raccrocher ou laisser un message ? Si message, lequel ? [`repondeur`]

## 6. Le suivi

20. Étapes du pipeline Axonaut : pour un RDV, un rappel, un barrage, un refus, un hors-cible ?
    Le pipeline existe-t-il déjà ? [`axonaut`]
21. Nombre d'essais et délai entre deux essais ? [`max_tentatives`, `delai_entre_tentatives_h`]
22. Volume prévu et date de démarrage souhaitée ?
