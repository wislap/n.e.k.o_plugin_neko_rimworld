# Direct collection query revision (public_query.v2)

A single query can be sent directly as {from,args,where,select,...}; requests:[...] batches the identical shape under one sample. Mixing the two forms is rejected.

The current Agent interface exposes Pawn.inventory, equipment, apparel and carried directly as Thing collections. Their populations remain distinct; selection, conditions, paging and aggregates are identical. Tracker and inner-container fields remain in the developer GraphQL schema, not the Agent field directory. Thing.contents explicitly reads a container through the same rule. Null intermediate containers remain null; a null final carried Thing is an observed empty collection.

Grouped queries may select values outside group_by. The executor verifies that requested values are shared within the group and returns them in rows.values. Shared object identities avoid repeated projection. Different values return null with group_field_varies, never an arbitrary representative. When enumeration is incomplete, shared values are unknown. Only displayed groups project their fields; all matching objects still contribute to complete aggregates.

Example (ordinary collection operations, not an inventory preset):

```json
{"requests":[{"from":"things","args":{"map_id":"current","include_held":false},"where":{"definition.category":{"eq":"Item"}},"group_by":["definition.def_name"],"select":{"definition":["def_name","label","is_weapon","is_ingestible"]},"aggregate":{"quantity":{"op":"sum","field":"stack_count"}}},{"from":"spawned_free_colonists","args":{"map_id":"current"},"select":["id","name","inventory","equipment","apparel","carried"]}]}
```

The v1.4.2 integration keeps v2. Selection arrays can mix string paths and
field maps, for example `['id', {'definition': ['def_name', 'label']}]` (shown as
JavaScript notation). A bare relation map is a child selection only if every
key is an actual child field and none is `select/args/where/aggregate/group_by/
order_by/limit/offset/on`. Otherwise it must obey the explicit options grammar.
Duplicate/overlapping fields, unknown names and misplaced parameters are
rejected; arguments and filters are never silently relocated. Compiler errors
include their input path and owning type where available; some condition and
root-envelope errors still identify only a containing object or message.

The common-field descriptions and additional `field_notes` are generated from
the same registry used by `describe_query`. Notes carry existing nonempty
field descriptions; this is not a claim of complete units or enum metadata
for every field. Text submission uses only `{text}` parts, top-level language,
and the existing answer/detail contract in both ordinary calls and fallback.

This revision changes the canonical shape, so it declares v2. Saved v1 observations are not rewritten. The plugin can connect to either advertised version. Native identity and scope rules below remain valid; v1 tracker traversal examples below are historical.

The v1.6 query/execution revision keeps v2. Root argument branches are generated
from the same registered arguments. Definition predicates may be safely projected
through all/any/not before Thing enumeration; mixed or unknown branches retain
three-valued behavior. Instance-only filters and display pagination do not promise
to lower source candidate scans. Root and nested collection scopes retain actual
arguments and their own where; shared relation descriptions are returned once.

The runtime enforces a research deadline and a submission-only finalization phase.
The experimental working-data view and task-goal wording were tested in production
code, then withdrawn from defaults after inconclusive correctness confirmation.
Append-only observations and audit remain the authoritative record.

---

# Shared v1.4 query data path

The current Mod advertises `public_query.v2` (the initial v1.4 revision used v1). The plugin calls `query` and `describe_query` internally; the model sees only the semantic tools (`look`, `count` and the catalog-dependent `inspect_*`, `survey_space` and `evaluate_activity` tools), never legacy templates, GraphQL or the separate resource tool. Older connected Mods retain their existing adapter. Legacy transports below are retained for compatibility/development, not a second recommended Agent workflow.

When advertised by the connected Mod, `spatial_query.v1` is exposed by the
semantic plugin as `inspect_space(from,to)`. This is a single current-tick,
read-only native reachability check from one Pawn to one Thing or map cell.
References accept exact names or IDs directly; ambiguity returns candidates and
never silently selects one. The operation does not infer WorkGiver eligibility,
reservation, route distance, Room/Region membership or future job success.

## Single executable field vocabulary

`PublicFields` derives canonical snake_case names, native mappings, types, arguments and operations from the executable Mod schema. `def` becomes `definition`; `def_name`, instance `id`, and display `label` remain distinct. Python forwards requests without renaming game fields or maintaining classification tables. Public conditions translate to the same typed condition evaluator used by existing native reads. Internal `fields/types/arguments/match` aliases are rejected.

With the v1.6.1 Mod, single-object conditions accept a pure child-field map or
an explicit `where` wrapper. Collections still require `some/every/none`.
Concrete `on` selections accept the same field arrays/maps and explicit selection
options as related objects. Structural discriminator errors are reported before
dependent field errors. Batch compilation reports one primary error per invalid
root request and lists indices that passed validation; the entire batch remains
unexecuted if any request is invalid. These diagnostics reach the model in the
existing error message and issues. They do not represent successful game reads.

The Mod directory now also supplies `value_domains` for all ten exposed native
enum types. Each field points to its own domain; `describe_query` accepts either
an object type or the full native domain name (for example `Verse.ThingCategory`).
The plugin forwards this metadata without a separate enum table. Wire values
remain strings. Enum conditions accept `eq/ne/in/is_null`; native flags additionally
accept comma-separated declared names and `has_all/has_any`. Default matching is
case-sensitive; `case_sensitive:false` permits ordinal case folding. Invalid
literals fail before reads, while unknown observed native values retain unknown
membership. Names, labels and Def identifiers remain open strings. A valid empty
query is still zero; no matching record cannot establish an arbitrary string as a
valid category.

`query({requests:[...]})` selects a root, its `args`, optional `where`, and fields in `select`. Related objects are selected directly; collection transport `items/nodes` windows are not part of selection paths (a native collection actually named `items` remains a field). Queries can include counts, declared additive sums, min/max and grouping. Filtered detail and aggregates use the same candidate collection; aggregates precede display `limit/offset`. Selected objects return scalar fields and parameter-free collections by default; wrapper-only objects expand their related fields, so selecting inventory does not return a misleading empty object. Long descriptions and argument-dependent scans remain explicit. Objects accept projection options; collection-only options on single objects or concrete projection branches are rejected before any game read.

```json
{"requests":[{"from":"things","args":{"map_id":"Map_0","visible_only":false},"where":{"definition.category":{"eq":"Item"}},"group_by":["definition.def_name","definition.label"],"aggregate":{"quantity":{"op":"sum","field":"stack_count"},"records":{"op":"count"}}}]}
```

This is an example of an explicit native category filter, not a hidden supplies preset. `IsWeapon` includes improvised weapons; `CountAsResource` is readout membership, not ownership or usefulness.

## Populations and relationships

- `things`: selected loaded maps, spawned indexed Things and optionally their declared holder trees; default `include_held:true`, `include_unreal:false`, `visible_only:true`. It is not all world/caravan possessions or proof of player ownership.
- `pawns`: all reachable pawns on selected loaded maps, using the native Pawn index and holder trees. `spawned_free_colonists` retains its narrower explicitly named population.
- `pawn(id)`: finds the returned pawn identity across loaded-map pawns.
- Map selectors accept `map_id:"current"` (map root `id:"current"`) and return the resolved ID in scope. This is available across roots with map selectors, without an extra game/maps read.
- `map(id)`: storage groups and the separate original ResourceCounter.
- Native unreal contents follow `ThingOwnerUtility.AreImmediateContentsReal`; excluding a corpse/minified inner object does not exclude all its descendants. Explicit `include_unreal:true` is available for inspection and must not be blindly added to wrapper counts.
- `storage_group` is storage-cell membership, overlapping spawned-map membership; it is not acceptance by current storage settings. `map`/`map_held`, `position`/`position_held`, and holding relations remain distinct.

The game computes physical ID deduplication. Ordinary nonphysical collections retain their entries; grouping keys and independent requests are not assumed to form disjoint populations.

## Results and limits

Each result reports effective scope and predicate, `scan_complete`, `matched_count` (null when incomplete), `known_matches`, `fields_complete` and explicitly typed aggregates. Requested detail includes `rows` and `page`; an aggregate query with `limit:0` omits both, including in nested collections. This means detail was not requested, not that scanning was complete. Consumers must treat these two fields as optional for aggregate-only responses. A partial aggregate has no exact value. A display limit does not reduce an already complete aggregate. Empty complete count/sum is zero; empty min/max is null.

Every call/continuation is a fresh observation; IDs can disappear after stack split/merge. Production reads remain on the game thread with a 12ms read budget. Batches are bounded to 8 requests, depth12/512 selections, 8192 candidates per collection, 20000 projected objects and 80000 result characters. Wider requests can be partial; a frozen 5s reader is not evidence of production timing.

Complete raw observations and execution requests remain in audit storage. The model receives compact data, scope and issues. The new contract uses the existing text-only answer-part composition, not descriptor/path references: answer is the short conclusion, detail carries the full factual report. No new submission parameters were added. Metadata discovery is not a live fact observation. No mutation, automatic pause or background world mirror is added.

## Evaluation

The existing paired version runner seals each plugin and query DLL separately. `bench.py experiment queries` checks fixed queries against native-graph reference values; `experiment versions --plan` runs the actual Agent. Owner-dependent native projections (body parts, capacities, backstories, computed stats) are captured separately for each pawn. Limited numeric evidence adapters validate matching scopes; complex/open answers remain explicit review cases. Delivery and software tests are not semantic quality scores.
