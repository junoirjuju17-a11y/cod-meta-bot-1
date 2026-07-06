# COD META Bot

Bot Discord Python qui surveille automatiquement [WZStats](https://wzstats.gg/fr) et publie les nouvelles armes META dans un salon Discord.

## Fonctionnalités

- Vérification automatique toutes les 10 minutes.
- Scraping dynamique avec Playwright.
- Publication Discord sous forme d'embed.
- Anti-doublon persistant avec SQLite.
- Commandes slash :
  - `/meta` : liste actuelle des armes META.
  - `/top10` : 10 meilleures armes.
  - `/arme <nom>` : détails d'une arme.
- Compatible Docker et Railway.

## Variables d'environnement

Variables obligatoires :

```env
DISCORD_TOKEN=token_du_bot
CHANNEL_ID=1523456954121588766
```

Variables optionnelles :

```env
GUILD_ID=1224678261154386001
DATABASE_PATH=data/meta.sqlite3
WZSTATS_URL=https://wzstats.gg/fr
CHECK_INTERVAL_MINUTES=10
LOG_LEVEL=INFO
```

`GUILD_ID` est recommandé pour synchroniser les commandes slash immédiatement sur un serveur précis.

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

## Déploiement Railway

1. Pousser le projet sur GitHub.
2. Créer un service Railway depuis le dépôt GitHub.
3. Ajouter les variables `DISCORD_TOKEN` et `CHANNEL_ID`.
4. Déployer.

Le `Dockerfile` installe Chromium automatiquement via Playwright. Aucune commande manuelle `playwright install chromium` n'est nécessaire sur Railway.

## Persistance SQLite

Par défaut, la base est créée dans :

```text
data/meta.sqlite3
```

Pour une persistance durable sur Railway, ajouter un volume Railway et définir `DATABASE_PATH` vers un chemin monté par ce volume, par exemple :

```env
DATABASE_PATH=/data/meta.sqlite3
```

Sans volume, la base peut être recréée lors d'un redéploiement complet.

## Notes

WZStats charge son contenu dynamiquement. Le scraper utilise donc Playwright, attend le contenu visible, extrait les cartes d'armes, puis enrichit les premières armes avec les détails disponibles sur leurs pages.

Le bot utilise l'API réseau de Playwright pour lire le HTML renvoyé par WZStats, sans ouvrir Chromium pendant l'exécution. C'est plus stable sur Railway et évite les crashs `Target crashed`.

Si WZStats est temporairement inaccessible, le bot journalise l'erreur et réessaie au cycle suivant sans s'arrêter.
