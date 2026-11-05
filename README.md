# Playlist Transfer 🎵

Application web pour **copier une playlist d'une plateforme de streaming à une autre** :
Spotify, Deezer, YouTube Music et Apple Music.

Tu colles le lien d'une playlist, tu choisis la plateforme de destination, l'application
retrouve chaque morceau sur cette plateforme et recrée la playlist sur ton compte.

> L'application copie la **liste** des morceaux (titre, artiste, album, durée, ISRC) et
> recrée la playlist avec les morceaux du catalogue de la plateforme cible.
> Aucun fichier audio n'est téléchargé.

## Fonctionnement

1. **Colle le lien** : `open.spotify.com/playlist/…`, `deezer.com/playlist/…`,
   `music.youtube.com/playlist?list=…`, `music.apple.com/…/playlist/…`
   (les liens courts `spotify.link` / `link.deezer.com` sont acceptés).
2. **Choisis la destination** et connecte ton compte (bouton « Connecter mon compte »).
3. **Vérifie les correspondances** : pour chaque morceau, l'application cherche
   - d'abord par **ISRC** (code unique de l'enregistrement → correspondance exacte),
   - sinon par **recherche texte** avec un score de ressemblance qui tient compte du titre
     nettoyé (« Remastered », « feat. », « Clip officiel »…), des artistes et de la durée,
     et qui pénalise les mauvaises versions (live, remix, acoustique…).

   Tu peux décocher une correspondance douteuse, afficher les introuvables et les copier.
4. **Crée la playlist** : elle est créée en privé sur ton compte, le lien s'affiche.

Bonus : export de la playlist source en texte ou en CSV.

## Synchronisation automatique ↻

Une fois la copie créée, active **« Garder les deux playlists à jour »** : le serveur vérifie
régulièrement les deux playlists (toutes les heures, 6 h, une fois par jour, par semaine…)
et reporte les changements.

- **Original → copie** : l'original sert de référence. Les morceaux ajoutés ou retirés de
  l'original le sont aussi dans la copie ; ce que tu ajoutes à la main dans la copie est conservé.
- **Dans les deux sens** : un morceau ajouté ou retiré dans l'une des deux playlists l'est aussi
  dans l'autre. Il faut être connecté aux deux plateformes, et l'original doit t'appartenir.
- **Reporter aussi les suppressions** : option à décocher si tu veux que la synchronisation
  ne fasse qu'ajouter.

Comment ça marche : à chaque vérification, l'application relit les deux playlists et les compare
à la « photo » prise lors de la vérification précédente. Ce qui est apparu a été ajouté, ce qui a
disparu a été retiré ; chaque changement est reporté de l'autre côté en retrouvant le morceau
équivalent (ISRC puis recherche). Les correspondances déjà trouvées sont mémorisées pour ne pas
refaire les recherches.

Dans la section **Mes synchronisations** : résultat de la dernière vérification (+ / − morceaux),
erreurs éventuelles, bouton « Synchroniser maintenant », changement de fréquence ou de sens,
pause, suppression. Tu peux aussi **relier deux playlists qui existent déjà** : la première
vérification les fusionne (sans doublons), puis elles restent synchronisées.

Sécurités :
- si plus de la moitié d'une playlist semble avoir disparu d'un coup (souvent une erreur de
  lecture), les suppressions ne sont pas reportées et un avertissement s'affiche ;
- Apple Music ne permet pas de retirer un morceau via son API : les suppressions à faire dans
  une playlist Apple Music sont signalées pour que tu les fasses à la main ;
- sur YouTube, chaque morceau ajouté ou retiré consomme du quota (≈ 190 opérations par jour).

Les connexions aux comptes et les synchronisations sont enregistrées dans `data/app.db`
(SQLite), pour continuer après un redémarrage du serveur. **Le serveur doit rester allumé** pour
que les vérifications automatiques aient lieu (sur un ordinateur allumé, un Raspberry Pi ou un
petit serveur en ligne).

## Ce que chaque plateforme permet

| Plateforme     | Lire une playlist (source)                         | Créer une playlist (destination)        |
|----------------|----------------------------------------------------|-----------------------------------------|
| Spotify        | Clé Spotify requise ¹                               | ✅ connexion Spotify                    |
| Deezer         | ✅ sans clé (playlists publiques)                   | ✅ connexion Deezer ² (clé requise)      |
| YouTube Music  | ✅ sans clé (playlists publiques)                   | ✅ connexion Google ³                   |
| Apple Music    | ✅ sans clé (page publique) — fiable avec une clé   | ✅ connexion Apple Music ⁴ (clé requise) |

1. Depuis février 2026, les applis Spotify en « Development Mode » ne peuvent lister que
   les morceaux des playlists dont l'utilisateur connecté est **propriétaire ou
   collaborateur**, et le propriétaire de l'appli doit avoir Spotify Premium. Pour copier la
   playlist de quelqu'un d'autre depuis Spotify : connecte ton compte, ou ajoute-la d'abord à
   ta bibliothèque. L'application gère les nouveaux chemins de l'API (`/items`, `/me/playlists`)
   et retombe sur les anciens si besoin.
2. Deezer n'accepte plus toujours la création de nouvelles applications. Sans clé, Deezer
   reste utilisable comme **source**.
3. L'API YouTube a un quota gratuit de 10 000 unités/jour ≈ **190 morceaux ajoutés par jour**.
   Au-delà, la playlist est créée mais incomplète : relance le lendemain avec les morceaux restants.
4. Nécessite un compte Apple Developer (payant) pour la clé MusicKit. La connexion se fait
   dans le navigateur avec MusicKit JS ; la playlist est créée dans ta bibliothèque Apple Music.

## Installation

Prérequis : Python 3.10 ou plus récent.

```bash
git clone <ce dépôt> && cd Project-music
python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # puis remplis les clés des plateformes voulues
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Ouvre ensuite **http://127.0.0.1:8000** dans ton navigateur.

## Obtenir les clés

Pour chaque plateforme, déclare l'URI de redirection `http://127.0.0.1:8000/auth/<plateforme>/callback`
(remplace par ton domaine si tu héberges l'application, et mets à jour `BASE_URL`).

- **Spotify** — [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) →
  *Create app* → coche *Web API* → URI de redirection `http://127.0.0.1:8000/auth/spotify/callback`.
  Copie *Client ID* et *Client secret* dans `.env`. En mode développement, ajoute les comptes
  autorisés dans *User Management*.
- **Deezer** — [developers.deezer.com/myapps](https://developers.deezer.com/myapps) →
  *Create a new application* → *Redirect URL* `http://127.0.0.1:8000/auth/deezer/callback`.
- **YouTube Music** — [console.cloud.google.com](https://console.cloud.google.com/) →
  active *YouTube Data API v3* → *Identifiants* → *ID client OAuth* de type *Application Web*
  → URI de redirection `http://127.0.0.1:8000/auth/youtube/callback`. Tant que l'appli est en
  mode « Test », ajoute ton adresse Gmail dans *Utilisateurs test*.
- **Apple Music** — [developer.apple.com](https://developer.apple.com/account/resources/authkeys/list) →
  *Keys* → nouvelle clé avec *Media Services (MusicKit)* → télécharge le fichier `.p8`.
  Renseigne `APPLE_TEAM_ID`, `APPLE_KEY_ID` et `APPLE_PRIVATE_KEY_PATH`.

## Structure du code

```
app/
  main.py          serveur web (FastAPI) : pages, connexions OAuth, API
  links.py         reconnaissance des liens de playlists
  matching.py      nettoyage des titres et score de ressemblance
  transfer.py      recherche de tous les morceaux sur la plateforme cible
  sync.py          synchronisation périodique original <-> copie
  storage.py       enregistrement des connexions et synchronisations (SQLite)
  providers/       une classe par plateforme (lecture, recherche, création)
    spotify.py  deezer.py  youtube.py  apple.py
static/            interface (HTML / CSS / JavaScript)
tests/             tests automatiques (HTTP simulé, aucun accès réseau)
```

Ajouter une plateforme = écrire une classe `Provider` (voir `app/providers/base.py` :
lecture, recherche, `create_empty`, `add_tracks`, `remove_tracks`),
l'ajouter dans `app/providers/__init__.py` et reconnaître ses liens dans `app/links.py`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Limites connues

- Les synchronisations sont liées au navigateur qui les a créées (cookie de session valable
  un an) : ouvre l'application depuis ce même navigateur pour les gérer.
- Un morceau absent du catalogue de la plateforme cible (exclusivité, restriction de pays)
  ne peut pas être ajouté ; il apparaît dans la liste des introuvables.
- La lecture d'Apple Music sans clé passe par la page publique, dont le format peut changer.
