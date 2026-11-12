# Ma veille (entrepreneuriat, reprise d'entreprise…)

Le titre et le surtitre de la page se règlent dans `sources.json` (`site_title`, `site_kicker`). Le dépôt et l'adresse
de la page gardent le nom d'origine `veille-entrepreneuriat` (renommer le dépôt casserait l'ancienne adresse).

Page unique (`site/index.html`) régénérée chaque matin par **GitHub Actions** et publiée sur **GitHub Pages** :
<https://thpmarc.github.io/veille-entrepreneuriat/>. Python 3.13, bibliothèque standard uniquement : rien à installer.
La page porte `noindex` : elle n'est pas destinée à être référencée par les moteurs de recherche.

## Comment ça tourne

- `.github/workflows/update.yml` s'exécute chaque jour (~7 h 17 à Montréal l'été, 6 h 17 l'hiver ; GitHub peut décaler
  de quelques minutes) et à la demande (onglet *Actions* → *Mise à jour de la veille* → *Run workflow*).
- L'historique des articles (`archive.json`, `status.json`) vit sur la branche **`data`**, réduite à un seul commit
  réécrit chaque jour : `main` ne contient que le code. Si cette branche est supprimée, le workflow échoue
  volontairement (sinon l'historique repartirait de zéro sans prévenir).
- **Alertes** : quand une nouvelle publication apparaît dans l'une des revues, le workflow ouvre une *issue* assignée au
  propriétaire du dépôt et le mentionnant, ce qui déclenche l'e-mail (et la notification mobile) de GitHub. Une alerte dont
  l'envoi échoue reste « pending » dans l'archive et repart au passage suivant. Test : *Run workflow* en cochant
  « Envoyer une alerte de test ». (Aucune alerte au tout premier lancement : l'existant n'est pas « nouveau ».)
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

## Onglets-sections (« Reprise MTL », « Finance »)

Chaque onglet-section est décrit dans `settings.tabs` de `sources.json` (`id`, `kind`, `label`, `title`, `groups`, `show`,
`new_days`, `manual`). Les sources qui portent le même `"kind"` y sont rangées par `"group"`, une section par source. Leurs
articles n'apparaissent pas dans « Aujourd'hui » ni « Cette semaine ». Pour ajouter un onglet : une entrée dans `tabs`,
des sources avec ce `kind`, et `keep_days_<kind>` dans `settings` (sinon la durée de la presse s'applique).
Un clic sur un titre, ou sur « Marquer lu », atténue l'élément (état gardé dans le navigateur, pas synchronisé).

### Reprise d'entreprise à Montréal (`kind: reprise`)

| Source | Méthode |
|---|---|
| Accounting Practice Sales · Québec | lecture de la page d'annonces (`type: aps`), Québec seulement ; `live: true` : une annonce qui quitte le site disparaît d'ici |
| Repreneuriat Québec | flux RSS (webinaires, témoignages, conseils) |
| FCEI | flux RSS, filtré sur la relève / le transfert (`match`) |
| PME MTL, Raymond Chabot Grant Thornton, Mallette | pas de flux : Google Actualités avec `site:domaine`, filtré par `match` |
| Presse (3 requêtes) | Google Actualités : « repreneuriat Québec », « repreneuriat Montréal », « transfert entreprise relève Québec » |

- `match` : expression régulière appliquée au titre **sans accents ni majuscules**. Mettre `\b` devant `repren` :
  « entrepreneuriat » contient « repren ».
- `manual_sites` (fin de `sources.json`) : sites sans flux ni page lisible par robot (Index, BizBuySell, BizQuest, Fusacq,
  Sunbelt, BDC, Fonds FTQ…), affichés en liste « À consulter à la main », sans mise à jour automatique.
- Premier passage d'une nouvelle source : ses éléments ne sont pas « Nouveaux » (annonces sans date marquées `baseline`).
- Test sans toucher aux autres sources : `python build_site.py --only r-aps,r-pmemtl`.

### Finance : Québec, France, international (`kind: finance`)

Sources en trois groupes. « Nouveau » = détecté aujourd'hui (`new_days: 1`), conservation 30 jours (`keep_days_finance`).

| Groupe | Sources |
|---|---|
| Québec et Canada | Banque du Canada (flux des communiqués), La Presse Affaires, Le Devoir Économie, Journal de Montréal Argent, et 3 requêtes Google Actualités (taux directeur, financement des PME, fiscalité des entrepreneurs) |
| France | Le Monde Économie, Le Figaro Économie, BFM Économie, JDN Économie, AMF (flux), et Google Actualités : Les Échos (`site:lesechos.fr`, pas de flux), Banque de France (`site:`, le site renvoie 403), financement des PME, fiscalité des indépendants |
| International | BCE, Réserve fédérale, The Economist, Financial Times (titres), RFI Économie, France 24 Éco-Tech, et Google Actualités FMI/OCDE/Banque mondiale |

Écartés : AMF Québec et FMI/OCDE (403), La Tribune, Capital, Boursorama, Zonebourse (pas de flux), Statistique Canada (trop
généraliste), JDN Finance et épargne (doublon de JDN Économie).

## Dépannage

- Retirer une source de `sources.json` supprime aussi ses articles de l'archive à la prochaine mise à jour.
- Sources refusées aux robots (403), non contournées : Bpifrance Le Hub, Les Affaires, BizBuySell, BizQuest, Fusacq.
