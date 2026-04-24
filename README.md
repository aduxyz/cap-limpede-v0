# Cap Limpede

Prototip pentru un corector de știri în limba română.

## Ce face acum

- backend Python simplu, fără framework extern
- autodetect pentru input:
  - text simplu
  - HTML
  - JSON simplu cu `title` și `content`
- prompt compus din:
  - `A` editabil
  - `B` fix, pentru formatul JSON de output
- integrare cu OpenAI Chat Completions prin `response_format=json_schema`
- fallback local pentru demo, dacă lipsește `OPENAI_API_KEY`
- frontend în 2 pași:
  - pas 1: input știre + prompt
  - pas 2: review original vs propus

## Pornire locală

```bash
cd /Users/adu/work/O/cap-limpede
python3 app.py
```

Apoi deschizi:

```text
http://127.0.0.1:8000
```

Dacă portul e ocupat:

```bash
CAP_LIMPEDE_PORT=8017 python3 app.py
```

## Variabile utile

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `CAP_LIMPEDE_HOST`
- `CAP_LIMPEDE_PORT`

## Structură

- `app.py` - backend HTTP și integrarea cu OpenAI
- `index.html` - frontend cu pas 1 + pas 2
