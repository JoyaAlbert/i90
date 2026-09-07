# I90 — REE/eSIOS daily ingestion

Pipeline diario para descargar y preparar **I90DIA** de Red Eléctrica/eSIOS para análisis de inteligencia de mercado.

## Qué hace

1. Calcula `D_objetivo = hoy (Europe/Madrid) - 90 días`.
2. Busca `I90DIA` (`archive_id=34`) por **fecha de datos**.
3. Si D no está disponible, retrocede hasta 7 días.
4. Descarga el fichero real.
5. Extrae ZIP si procede.
6. Detecta CSV, encoding y separador.
7. Guarda:
   - fichero bruto como artifact de GitHub Actions;
   - `manifest.json`;
   - `schema.json`;
   - `preview.csv`;
   - histórico ligero por fecha.
8. Mantiene una base para análisis 7d/30d y mapping UP -> agente -> UF -> tecnología.

## Configuración

Crea este secret en el repositorio:

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `ESIOS_API_KEY`
- Secret: tu token de eSIOS

No metas la API key en código, commits, issues ni logs.

## Ejecutar localmente

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
export ESIOS_API_KEY="..."
python -m i90_ingest.cli
```

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
$env:ESIOS_API_KEY="..."
python -m i90_ingest.cli
```

## GitHub Actions

Workflow: **I90 daily ingest**

- ejecución diaria antes del briefing;
- `workflow_dispatch` manual;
- publica el bruto como artifact;
- versiona manifest/schema/preview e histórico ligero.

## Estructura

```text
.github/workflows/i90-daily.yml
src/i90_ingest/
  api.py
  archive.py
  csv_tools.py
  pipeline.py
  cli.py
tests/
public/
```

## Importante

El parser inicial es deliberadamente conservador: primero inspecciona el CSV real y genera schema/preview. No inventa columnas ni semántica de bloques que no hayan sido observados.
