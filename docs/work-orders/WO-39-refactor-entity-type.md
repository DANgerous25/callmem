# WO-39 — Refactor Entity Type

## Goal

Add "refactor" as an entity type so code refactoring work is tracked distinctly from features, bugfixes, and changes.

## Background

claude-mem has a "refactor" observation type. callmem currently has: decision, todo, fact, failure, discovery, feature, bugfix, research, change. Refactoring is conceptually different from a feature (no new behaviour) and from a change (implies structural improvement, not just modification).

## Deliverables

### 1. Add type to `EntityType` literal

In `src/callmem/models/entities.py`:

```python
EntityType = Literal[
    "decision", "todo", "fact", "failure", "discovery",
    "feature", "bugfix", "research", "change", "refactor",
]
```

### 2. Update extraction prompt

In the extraction prompt template, add "refactor" to the list of entity types with description:

```
- refactor: Code restructuring that improves internal quality without changing external behaviour (rename, extract function, simplify logic, reduce duplication)
```

### 3. Web UI badge colour

Add a badge colour for "refactor" in the UI CSS/template. Suggested: teal or slate — visually distinct from existing badges.

### 4. No migration needed

The `type` column is TEXT, not an enum constraint. Existing databases accept the new value without schema changes.

## Constraints

- Python 3.10 compatible
- No AI attribution
- Trivial change — should be < 30 minutes

## Acceptance criteria

- [ ] "refactor" accepted as entity type in model validation
- [ ] Extraction prompt includes "refactor" type with description
- [ ] Web UI shows "refactor" badge with appropriate colour
- [ ] All existing tests pass
