# Veille entrepreneuriat

Page unique (`site/index.html`) régénérée chaque matin par **GitHub Actions** et publiée sur **GitHub Pages** :
<https://thpmarc.github.io/veille-entrepreneuriat/>. Python 3.13, bibliothèque standard uniquement : rien à installer.
La page porte `noindex` : elle n'est pas destinée à être référencée par les moteurs de recherche.

## Comment ça tourne

- `.github/workflows/update.yml` s'exécute chaque jour (~7 h 17 à Montréal l'été, 6 h 17 l'hiver ; GitHub peut décaler
  de quelques minutes) et à la demande (onglet *Actions* → *Mise à jour de la veille* → *Run workflow*).
- L'historique des articles (`archive.json`, `status.json`) vit sur la branche **`data`**, réduite à un seul commit
  réécrit chaque jour : `main` ne contient que le code. Si cette branche est supprimée, le workflow échoue
  volontairement (sinon l'historique repartirait de zéro sans prévenir).
- Une source en erreur n'empêche pas les autres : voir « État des sources » en bas de la page, et le journal du workflow.

| Fichier | Rôle |
|---|---|
| `sources.json` | liste des sources (ajouter / retirer / régler `max_items`, `filter`) |
| `build_site.py` | récupération des sources + génération de la page |
| `template.html` | style et interactions de la page |
| `.github/workflows/update.yml` | planning quotidien, sauvegarde de l'archive, publication Pages |

Essai en local : `python build_site.py` (réseau) ou `python build_site.py --no-fetch` (depuis `data/archive.json`),
puis ouvrir `site/index.html`. `--if-stale HEURES` saute la mise à jour si la dernière a moins de HEURES heures.

## Les 4 revues (onglet « Revues »)

| Revue | Méthode |
|---|---|
| Entreprendre & innover | flux RSS (`entreprendreetinnover.com`, ancienne adresse wordpress.com redirigée) |
| Revue de l'Entrepreneuriat (Cairn) | API publique Crossref, ISSN 1766-2524 — Cairn bloque les robots (DataDome) |
| Revue Gestion (HEC Montréal) | pas de flux RSS : lecture de la page `/entrepreneuriat/`, requêtes espacées de 2,5 s (conforme au robots.txt) |
| Management international | flux RSS |

Une revue publie rarement : « Cette semaine » y affiche ce qui est **nouveau pour vous** (détecté lors d'une mise à jour),
l'onglet « Revues » montre toujours les 8 dernières parutions de chacune.

## Dépannage

- Retirer une source de `sources.json` supprime aussi ses articles de l'archive à la prochaine mise à jour.
- Sources refusées aux robots (403), non contournées : Bpifrance Le Hub, Les Affaires.
