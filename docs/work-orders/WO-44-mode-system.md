# WO-44 — Mode System (Pluggable Taxonomies)

## Goal

Allow llm-mem to use different extraction taxonomies depending on the type of project — coding, research, documentation, data analysis — so entity types and extraction prompts are domain-appropriate.

## Background

claude-mem has a mode system with pluggable taxonomies (code, email, law). llm-mem currently has a single hardcoded taxonomy optimized for coding projects. While coding is the primary use case, the entity types (feature, bugfix, refactor) don't make sense for a research project or documentation effort.

## Deliverables

### 1. Mode definition format

Each mode is a TOML or YAML file in a `modes/` directory:

```toml
# modes/coding.toml (default)
name = "coding"
description = "Software development projects"

[entity_types]
decision = "A design or architectural decision with reasoning"
todo = "A task to be done, with priority"
fact = "A durable fact about the codebase or domain"
failure = "Something that went wrong — error, crash, unexpected behaviour"
discovery = "Something learned during investigation or debugging"
feature = "New functionality added"
bugfix = "A bug found and fixed"
research = "Investigation or analysis of a problem"
change = "A modification to existing code or configuration"
refactor = "Code restructuring without behaviour change"

[extraction_prompt]
template = "prompts/coding_extraction.txt"

[concept_tags]
enabled = true
tags = ["how-it-works", "gotcha", "pattern", "trade-off", "problem-solution", "why-it-exists", "what-changed"]
```

```toml
# modes/research.toml
name = "research"
description = "Research and analysis projects"

[entity_types]
finding = "A research finding or conclusion"
hypothesis = "A hypothesis being tested"
evidence = "Supporting or contradicting evidence"
methodology = "An approach or method used"
source = "A reference, paper, or data source"
question = "An open question to investigate"
decision = "A decision about research direction"
todo = "A task to be done"
fact = "A durable fact"

[extraction_prompt]
template = "prompts/research_extraction.txt"
```

### 2. Config integration

```toml
[project]
mode = "coding"  # default
```

The setup wizard asks:

```
  Project type:
    1) coding — Software development (default)
    2) research — Research and analysis
    3) docs — Documentation and writing
    4) custom — Define your own entity types
  Choice [default: 1]:
```

### 3. Mode loading

At startup, load the mode definition and use it to:
- Set the `EntityType` validation list dynamically
- Load the appropriate extraction prompt template
- Configure concept tags (if enabled for the mode)

The `EntityType` literal becomes a runtime-validated string against the mode's type list, not a compile-time Literal. Use a validator function instead.

### 4. Custom mode support

If the user selects "custom", create a `modes/custom.toml` in the project's `.llm-mem/` directory where they can define their own types. Provide the coding mode as a starting template.

### 5. Built-in modes

Ship with:
- `coding` (current behaviour, default)
- `research` (findings, hypotheses, evidence)
- `docs` (sections, revisions, references, style decisions)

### 6. Mode switching

```bash
llm-mem config --mode research -p .
```

Changing modes does not affect existing entities — they keep their original types. New entities use the new mode's types. A migration/re-extraction can be done separately if needed.

## Constraints

- Python 3.10 compatible
- No AI attribution
- Backward compatible — existing projects with no mode config default to "coding"
- Mode definitions are read-only after loading (no runtime modification)
- Custom modes stored in `.llm-mem/modes/` (project-specific)
- Built-in modes stored in `src/llm_mem/modes/` (package data)

## Acceptance criteria

- [ ] `coding` mode produces the same entities as current behaviour
- [ ] `research` mode produces research-appropriate entity types
- [ ] Setup wizard offers mode selection
- [ ] `--mode` config switch works
- [ ] Custom mode definition works
- [ ] Existing projects without mode config default to "coding"
- [ ] All existing tests pass
