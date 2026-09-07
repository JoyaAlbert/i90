# I90 — REE/eSIOS daily ingestion + structural mapping

Pipeline diario para descargar **I90DIA** y construir un mapping trazable de
**UP → sujeto/agente → empresa → UF → tecnología**.

## Pipeline I90

1. Calcula `D_objetivo = hoy (Europe/Madrid) - 90 días`.
2. Descarga `I90DIA` (`archive_id=34`) por `date_type=datos`.
3. Retrocede hasta 7 días si D aún no está disponible.
4. Conserva el ZIP/CSV bruto como GitHub Actions artifact.
5. Publica manifest/schema/preview e histórico ligero.

## Pipeline estructural

Cada ejecución intenta obtener:

- eSIOS: Unidades de Programación.
- eSIOS: Unidades Físicas.
- eSIOS: Sujetos del Mercado.
- OMIE: `LISTADO DE UNIDADES OFERTANTES VIGENTES`.

Genera:

```text
public/structural/latest/
  programming_units.csv
  physical_units.csv
  market_subjects.csv
  omie_units.csv
  up_master.csv
  manifest.json
```

`up_master.csv` usa únicamente cruces exactos de código. No atribuye una empresa
por parecido del nombre de la UP.

### Grupos empresariales

Se separan:

- `legal_entity`: sociedad publicada por la fuente de mercado.
- `group_name`: grupo empresarial.
- `group_confidence`: cómo se obtuvo.
- `group_source_url`: fuente de la relación cuando se ha verificado.

Ejemplos incorporados con fuente corporativa:

- `AXPO IBERIA, S.L.` → `Axpo`.
- `GAS NATURAL COMERCIALIZADORA` → `Naturgy`.

Para otros grupos cuyo nombre aparece directamente en la sociedad (`ENDESA`,
`IBERDROLA`, `REPSOL`, etc.) se marca `derived_from_legal_name`, no como una
relación corporativa independiente verificada.

## Secret

En:

**Settings → Secrets and variables → Actions**

crea:

```text
ESIOS_API_KEY
```

Nunca guardes el token en código o commits.

## Ejecutar localmente

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
export ESIOS_API_KEY="..."
python -m i90_ingest.cli
python -m i90_ingest.structural_cli
```

## GitHub Actions

Workflow: **I90 daily ingest**

- ejecución diaria;
- ejecución manual;
- se ejecuta también ante cambios de código;
- no se vuelve a disparar cuando el bot solo actualiza `public/`;
- versiona `up_master.csv` diariamente.

## Principio de trazabilidad

El informe debe poder distinguir:

1. **HECHO**: código/empresa/UF publicado por REE/eSIOS u OMIE.
2. **MÉTRICA**: cálculo sobre I90.
3. **INFERENCIA**: estrategia comercial o agrupación empresarial no contenida
   literalmente en el dato de mercado.

Si una fuente estructural no puede descargarse o parsearse, `manifest.json`
registra el error y el pipeline no inventa el mapping.


## Mapping estructural v0.3

El mapping usa dos capas oficiales:

1. **OMIE `LISTA_UNIDADES.PDF`**: código exacto de unidad, descripción,
   agente propietario, porcentaje de propiedad, tipo, zona y tecnología.
   El parser usa coordenadas del PDF para evitar desplazamientos de columnas.
2. **eSIOS**: UP, UF y sujetos. Estas páginas son SPA y se renderizan con
   Chromium/Playwright antes de extraer la DataTable.

### Reglas de seguridad de datos

- Nunca se hace fuzzy matching por nombre.
- Una UP con varios propietarios genera varias filas y conserva
  `ownership_pct`.
- `group_name` está separado de `legal_entity`.
- Si eSIOS no se puede renderizar, OMIE sigue proporcionando el mapping
  UP → propietario → tecnología y el manifest deja `mapped_uf=0`.
- El informe no debe usar UF/subject mapping si el manifest marca error.


## Mapping estructural v0.5

Las páginas estructurales de eSIOS se usan únicamente para descubrir la
petición XHR/fetch real que alimenta cada tabla. El pipeline identifica el
JSON correspondiente por las filas visibles, reutiliza el endpoint interno y
pagina la API directamente.

El manifest deja trazabilidad de:

- `api_endpoint`
- `api_method`
- `api_record_path`
- `api_initial_records`
- `api_total_hint`
- `api_pagination_strategy`
- `api_pages`

La capa eSIOS solo queda `complete=true` si supera el umbral mínimo de filas y
la API capturada se ha agotado. Una primera página de 25 registros nunca se
acepta como cobertura completa.
