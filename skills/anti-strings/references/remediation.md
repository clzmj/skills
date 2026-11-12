# Remediation guidance

## Choose an owner

Use the closest existing module that owns the concept. Export a shared value only when multiple consumers truly use the same concept; keep local values local. Prefer a type-safe closed set when the domain is finite and the language/version/style supports it. Otherwise use a named constant. Reuse an existing import or domain module when that is the established boundary.

Keep constants near their domain instead of collecting unrelated values in a giant global bucket. Ordinary UI copy, CSS utility classes, import paths, and independent test/wire assertions usually should stay where they are. Same spelling can carry different meanings; consolidate only after checking callers and context.

Preserve serialized, API, and wire representations exactly. Cross-language values belong in an established schema or generated bindings when one already fits; a few unrelated strings do not justify introducing a generator.

## Small examples

Choose idioms compatible with the project's supported language version and conventions:

```go
type Status string

const (
	StatusOpen   Status = "open"
	StatusClosed Status = "closed"
)
```

```rust
enum Status { Open, Closed }
// If values cross a serialization boundary, retain the existing wire spellings.
```

```python
from enum import StrEnum  # Python 3.11+

class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
```

```ts
export const Status = { Open: "open", Closed: "closed" } as const;
export type Status = (typeof Status)[keyof typeof Status];
```

These are options, not a mandate to change representation. For example, retain established Rust serialization attributes and JavaScript/TypeScript wire values.

In plain JavaScript, a named export can provide the shared value without TypeScript syntax:

```js
export const Status = Object.freeze({ Open: "open", Closed: "closed" });
```

Some syntax requires literals: static import specifiers and Go struct tags or Rust serialization attributes cannot be replaced with identifier references. Keep those literals valid for their syntax and use constants at runtime boundaries. A Go typed constant does not make a runtime set closed, so validate untrusted inputs at the boundary. In Python `match`, bare `NAME` is a capture pattern; use qualified enum members such as `Status.OPEN` for constant patterns.
