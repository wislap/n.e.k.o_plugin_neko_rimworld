# World, faction, quest, trade and research consultation

These capabilities share the existing `rimworld_consult(question)` runtime,
ordinary JSON projection, observation audit, history and full report. They are
available when the connected Mod advertises `colony_state.v1`. A base connection
without this additive capability retains its current tools. No discovery round,
automatic five-domain background, separate agent or production switch is added.

## Direct reads

Use `look(object, aspects, limit?, offset?, focus?)`. Names resolve in the same
read; a directory lookup is not a prerequisite. Existing physical object reads
keep their existing contract.

| object.kind | aspects |
|---|---|
| world | places |
| world_objects | identity, location |
| caravans | identity, location, route, supplies |
| factions | identity, relations, settlements |
| quests | identity, requirements, rewards, deadlines |
| traders | identity, stock |
| research | progress, requirements, unlocks |

Selectors accept `id/ids/name/names`. World objects additionally accept category
and `near={kind:"map",map:"current"}` or a known object name/ID. Traders accept
map and `focus.items` (native item Def names or exact loaded labels). Research
names accept Def names or exact loaded labels. Untargeted lists use public
objects; caravans are player-controlled, quests are offered/ongoing and research
defaults to selected projects. Explicit public historical quest lookup is allowed.

Examples:

```json
{"object":{"kind":"quests","names":["补给请求"]},"aspects":["requirements","rewards","deadlines"]}
{"object":{"kind":"research","names":["MicroelectronicsBasics"]},"aspects":["progress","requirements","unlocks"]}
{"object":{"kind":"traders","names":["商船"]},"aspects":["stock"],"focus":{"items":["ComponentIndustrial"]}}
```

Top-level `total/page` describes the selected population. On a selected trader's
stock view, `offset/limit` page `stock.rows`; the selected trader is retained and
`stock.page.next_offset` continues its inventory. On an untargeted trader
directory they page partners, with each stock view starting at zero.

## Direct calculations

`evaluate_journey` takes an existing player caravan and all requested world
destinations. It uses native walking-caravan path and time estimates. Optional
supplies retain current carrying facts and route-specific food estimates. It
does not form a party, issue a route or predict encounters.

```json
{"caravan":{"name":"采购队"},"destinations":[{"name":"采购据点"},{"name":"备用据点"}],"include":["supplies"]}
```

`evaluate_trade` takes a trader, negotiator and explicit player buy/sell lists.
It resolves names, checks the supported channel and existing inventory, and
returns native contextual prices, aggregate currency rounding, available funds
and conditions. It neither substitutes goods nor performs a trade.

```json
{"trader":{"name":"商船"},"negotiator":{"name":"Lumi"},"buy":[{"item":"ComponentIndustrial","quantity":10}],"sell":[{"item":"WoodLog","quantity":40}]}
```

## Facts and scope

Planet locations retain layer and tile IDs. Faction identity is an instance ID,
not a FactionDef. Relations are relative to the player. Quest reward groups
remain alternatives; acceptance, delivery and public stage timers remain
distinct. Structured quest requirements cover supported native parts and retain
the public description for the rest. Offers are not proof of payment.

Research retains real/apparent progress, prerequisite chains, applied techprints,
bench/facility compatibility and known direct unlocks. Project selection is
separate from a researcher's actual work. Source definitions are not current
state. Existing settlement inventory is read without materializing missing stock;
market value is labeled separately from an actual quote.

All reads preserve sample/scope/unknowns and raw observations. Business status
such as an incomplete quote remains visible separately from transport success.
Native calculations are synchronous within the shared frame-operation lifetime;
these operations are not a hard-preemptible global planning service.

The current quick checks and provider-free SDK pipelines establish software and
native wiring, not live model quality, generalized token savings or deployment.
