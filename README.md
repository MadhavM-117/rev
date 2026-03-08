# Rev

Streamline code review for humans.

## How?

- Related changes are grouped together
- Changes are ordered in a meaningful manner, make it unfold like a story
- Extra context for changes are displayed directly in the diff.

### Questions

1. Does this require the use of AI? 
2. How much can you get done without the need for AI?

## Testing

Install test dependencies:

```bash
uv sync --extra dev
```

Run tests with coverage:

```bash
uv run pytest
```

Coverage reports:

- Terminal summary with missing lines (`term-missing`)
- XML report at `coverage.xml`
