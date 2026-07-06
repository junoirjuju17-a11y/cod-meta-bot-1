# COD META Bot

Bot Discord Python qui surveille les meilleures META Warzone affichees sur [WZStats](https://wzstats.gg/fr) et publie les changements dans un salon Discord.

## Fonctionnement

Toutes les 10 minutes, le bot :

- interroge `https://wzstats.gg/fr` avec Playwright ;
- recupere toutes les armes META disponibles ;
- selectionne uniquement la meilleure arme `Longue portée` et la meilleure arme `Courte portée` ;
- recupere le build META complet de ces deux armes ;
- stocke la selection courante dans SQLite ;
- compare les deux armes et leurs builds avec les donnees precedentes ;
- publie un embed quand la meilleure arme d'une portee change ou quand son build change.

Le bot enregistre les armes et les signatures de builds deja envoyees. Il ne republie jamais une arme ou un build deja publie.

## Donnees recuperees

Pour chaque arme selectionnee :

- nom ;
- tier ;
- type court, par exemple `AR`, `SMG`, `SNIPER` ;
- image ;
- accessoires META : bouche, canon, lunette, crosse, sous-canon, chargeur, poignee, laser, conversion et autres accessoires detectes ;
- lien WZStats.

## Commandes Discord

- `/meta` : affiche la selection META actuelle.
- `/top5` : affiche la selection META actuelle.
- `/arme <nom>` : affiche les details connus d'une arme selectionnee.

## Variables d'environnement

Variables obligatoires :

```env
DISCORD_TOKEN=token_du_bot
CHANNEL_ID=1523753006422818876
```

Variables optionnelles :

```env
GUILD_ID=1224678261154386001
DATABASE_PATH=data/meta.sqlite3
WZSTATS_URL=https://wzstats.gg/fr
CHECK_INTERVAL_MINUTES=10
LOG_LEVEL=INFO
```

`GUILD_ID` est recommande pour synchroniser rapidement les commandes slash sur un serveur precis.

## Railway

Le projet est compatible Railway via Docker.

Le `Dockerfile` installe Playwright et Chromium automatiquement. Aucune commande manuelle `playwright install chromium` n'est necessaire sur Railway.

Pour une persistance durable de SQLite sur Railway, ajouter un volume et definir par exemple :

```env
DATABASE_PATH=/data/meta.sqlite3
```

Sans volume, la base peut etre recreee lors d'un redeploiement complet.

## Lancement local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python main.py
```

Avec Docker :

```bash
docker build -t cod-meta-bot .
docker run --env-file .env cod-meta-bot
```
