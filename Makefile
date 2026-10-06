.PHONY: data test test-pipeline test-spec rank eval site serve check clean

## Regenerate all 8 scenarios into data/ (deterministic)
data:
	python3 -m exception_desk.simulator

## Run the test suite. 171 pass; 14 fail until rank_root_candidates is written.
test:
	python3 -m pytest tests/ -q

## Just the pipeline around the ranker. These must ALWAYS be green.
test-pipeline:
	python3 -m pytest tests/test_pipeline.py -q

## Just your spec. Red until step 3 is done -- this is the definition of done.
test-spec:
	python3 -m pytest tests/test_correlation.py -q

## Watch mode: reruns the correlation tests on save. Needs pytest-watch.
watch:
	ptw -- tests/test_correlation.py -q

## Dev loop for rank_root_candidates: your ranker vs two naive baselines
rank:
	python3 tools/rank_lab.py --mine --explain

## Score the engine across all scenarios and write data/scorecard.json
eval:
	python3 -c "from exception_desk.evaluate import run_evaluation; import json, pathlib; \
	card = run_evaluation(); \
	pathlib.Path('data/scorecard.json').write_text(json.dumps(card.to_dict(), indent=2)); \
	print(json.dumps(card.to_dict()['scenarios'], indent=2))"

## Copy generated data into web/data/ so the site can read it
site: data
	python3 tools/build_site_data.py

## Serve locally WITH the agent endpoint. Put ANTHROPIC_API_KEY in .env for live agents.
serve: site
	@python3 dev_server.py

## Static serve only, no agent endpoint (agents fall back to cached transcripts)
serve-static: site
	@cd web && python3 -m http.server 8000

## Everything that should pass before you push
check: test site
	@echo "ok"

clean:
	rm -f data/*.json web/data/*.json
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
