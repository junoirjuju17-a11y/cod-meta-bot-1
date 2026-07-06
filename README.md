# COD META Bot

Bot Discord Python qui surveille le Top 5 META Warzone affiche sur [WZStats](https://wzstats.gg/fr) et publie les nouvelles entrees dans un salon Discord.

## Fonctionnement

Toutes les 10 minutes, le bot :

- interroge `https://wzstats.gg/fr` avec Playwright ;
- recupere uniquement les 5 premieres armes META ;
- recupere le build META complet de chaque arme ;
- stocke le Top 5 courant dans SQLite ;
- compare le nouveau Top 5 et les builds avec les donnees precedentes ;
- publie un embed quand une arme entre dans le Top 5 ou quand un build change.

Le bot enregistre les armes et les signatures de builds deja envoyees. Il ne republie jamais une arme ou un build deja publie.

## Donnees recuperees

Pour chaque arme du Top 5 :

- nom ;
- tier ;
- type court, par exemple `AR`, `SMG`, `SNIPER` ;
- image ;
- accessoires META : bouche, canon, lunette, crosse, sous-canon, chargeur, poignee, laser, conversion et autres accessoires detectes ;
- lien WZStats.

## Commandes Discord

- `/meta` : affiche le Top 5 META actuel.
- `/top5` : affiche le Top 5 WZStats.
- `/arme <nom>` : affiche les details connus d'une arme du Top 5.

## Variables d'environnement

Variables obligatoires :

```env
DISCORD_TOKEN=token_du_bot
CHANNEL_ID=1523456954121588766
ROLE_ID=id_du_role_call_of_duty
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
`ROLE_ID` doit contenir l'identifiant du role Discord `Call of Duty`. Le bot envoie `<@&ROLE_ID>` avant chaque embed de nouvelle META afin que le ping fonctionne vraiment.

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
