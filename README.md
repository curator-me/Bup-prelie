# Bup-prelie

## Layout

```
app/
  schemas.py      request/response models
  guardrails.py   validation and safety checks
  interpreter.py  raw request -> structured representation
  optimizer.py    structured representation -> final result
  main.py         entry point
tests/
  sample_cases.json  example inputs with expected outputs
  test_samples.py    runs every sample case through the pipeline
```

## Run

```
pip install -r requirements.txt
python -m app.main
pytest
```
