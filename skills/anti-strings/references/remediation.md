# Typed value remediation

## Representation choices

Use the closest existing module, type, or schema as owner. Model finite domain choices as closed sets and structured data as typed records. Parse and validate external JSON, environment, CLI, HTTP, or database input once at its boundary, then pass typed models inward. Preserve exact serialized, API, and wire spellings. Share a value only when consumers share its meaning; do not consolidate ordinary UI copy, CSS classes, import specifiers, or independent test assertions. Prefer an existing schema or generated bindings for cross-language contracts.

The examples below are idiomatic options, not a language coverage guarantee or a mandate. Adapt them to supported language versions and local conventions.

| Language | Closed set | Record | Boundary parsing | Exhaustive match |
| --- | --- | --- | --- | --- |
| Python | `StrEnum` (3.11+) | `dataclass`; `TypedDict` for dictionary-shaped data | Validate/coerce at ingress (for example, Pydantic); `TypedDict` does not validate at runtime | `match` with qualified enum members; static exhaustiveness depends on tooling |
| TypeScript | `as const` object plus derived union, or string-literal union | `interface`/`type`; discriminated union for variants | Runtime schema such as Zod; types alone do not validate input | Narrow discriminants; check exhaustive switches |
| JavaScript | Frozen named object | Object/class with documented or runtime-checked shape | Runtime schema or explicit checks | No compile-time exhaustiveness |
| Go | Defined string type with named constants | `struct` (tags such as `json:"status"` remain literals) | Validate untrusted values at ingress | No enum exhaustiveness; handle defaults |
| Rust | `enum` | `struct` | Deserialize and validate; serde attributes preserve wire spellings | `match` is exhaustive |
| Java | `enum` | `record` (or class) | Parse and validate at ingress | Switch exhaustiveness depends on Java version and enum/sealed type |
| Kotlin | `enum class` or sealed hierarchy | `data class` | Validate during deserialization | `when` on sealed types/enums can be exhaustive |
| C# | `enum` or closed hierarchy | `record` | Validate deserialized input; enums admit undefined underlying integers | Switch may need a fallback |
| Swift | `enum` | `struct` | Decode with `Codable`, then validate constraints | `switch` is exhaustive |
| Ruby | Module constants or validated value object | `Data` (3.2+) or class | Validate at ingress | No built-in exhaustive enum checking |
| PHP | Backed `enum` (8.1+) | `readonly class` (8.2+) or typed class | Validate/coerce at ingress | Depends on language/tooling version |
| Elixir | Atoms for known choices; validate before converting untrusted strings | `defstruct` | Validate before atom conversion | Explicit `case` branches; handle unmatched input |
| SQL | `ENUM` or `CHECK` constraint; lookup table for shared sets | Table row with declared column types | Validate during application parsing; enforce with database constraints | Not applicable |

## Examples

```python
from dataclasses import dataclass
from enum import StrEnum

class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"

@dataclass(frozen=True)
class Ticket:
    status: Status
```

```ts
export const Status = { Open: "open", Closed: "closed" } as const;
export type Status = (typeof Status)[keyof typeof Status];

type Event = { kind: "created"; id: string } | { kind: "closed"; id: string };
```

```go
type Status string

const (
	StatusOpen   Status = "open"
	StatusClosed Status = "closed"
)

type Ticket struct { Status Status }
```

In Python `match`, bare names are capture patterns; use qualified enum members such as `Status.OPEN` for constant patterns. Keep literals required by syntax, such as static import specifiers, Go struct tags, and Rust serialization attributes. Add language-specific examples only when their behavior and minimum version have been checked; scanner support itself does not establish full language support.
