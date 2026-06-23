# web-crawler

A configurable, same-domain web crawler written in Python.

## Requirements

- Python 3.10+

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Usage

```python
from web_crawler.crawler import Crawler

crawler = Crawler()
result = crawler.fetch("https://example.com")
print(result.status_code, result.links)
```

Run with `PYTHONPATH=src` so the package under `src/` is importable.

## Development

```bash
ruff check .      # lint
mypy src          # type-check
pytest            # run tests
```

## Project structure

```
src/web_crawler/   # package source
tests/             # pytest test suite
.gitlab-ci.yml     # CI: lint, test, security scanning
```

## CI/CD

The pipeline runs linting, type-checking, tests, and GitLab security scanning
(SAST, Secret Detection, Dependency Scanning) on every push.

## License

MIT. See [LICENSE](LICENSE).
