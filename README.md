# Lot 3 – version éclatée

Structure proposée :

- `app.py` : orchestration Streamlit
- `models.py` : dataclasses
- `core/` : calculs métier et géométrie
- `services/` : APIs, météo, déclinaison, élévation
- `ui/` : carte, composants, état Streamlit

## Lancement

Place ce dossier à la racine de ton repo puis lance :

```bash
streamlit run app.py
```

## Note

Cette version est une répartition fonctionnelle du monolithe `app_optimized_lot1_lot2.py`.
