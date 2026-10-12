import { Alert, Button, Card, JsonView, Stack, StatusBadge, TextBlock } from "@neko/plugin-ui"
import type { SavedSituation } from "./types"

const kinds: Record<string, string> = { condition: "地图状况", threat_group: "敌对编组", event: "近期事件", alert: "游戏警报", quest: "任务" }
const sections: Record<string, string> = { conditions: "地图状况", threat_groups: "敌对编组", events: "事件", alerts: "警报" }
const reasons: Record<string, string> = {
  no_previous_read: "尚无比较基准", session_changed: "游戏会话已变化", map_changed: "地图已变化",
  incomplete_read: "读取不完整", clock_unavailable_or_rewound: "游戏时间缺失或已回退",
}

function sourceFacts(sources: SavedSituation["board"]["items"][number]["sources"]) {
  return sources.map(source => {
    const data = source.data
    if (!data || typeof data !== "object" || Array.isArray(data)) return ""
    const facts = [
      ["state", "状态"], ["priority", "等级"], ["since_tick", "开始 tick"], ["tick", "事件 tick"],
      ["ticks_left", "剩余 ticks"], ["scope", "范围"], ["lord_job", "活动"],
    ].flatMap(([key, label]) => data[key] === undefined || data[key] === null ? [] : [`${label}：${String(data[key])}`])
    if (Array.isArray(data.members)) facts.push(`成员引用 ${data.members.length} 项`)
    if (Array.isArray(data.subjects)) facts.push(`关联对象：${data.subjects.map(subject => typeof subject === "string" ? subject : JSON.stringify(subject)).join("、") || "无"}`)
    if (data.quest && typeof data.quest === "object" && !Array.isArray(data.quest)) {
      const quest = data.quest as Record<string, unknown>
      if (quest.state != null) facts.push(`任务状态：${String(quest.state)}`)
    }
    return facts.join(" · ")
  }).filter(Boolean).join("\n")
}

export function SituationCard({ situation, readEvidence }: { situation?: SavedSituation | null; readEvidence: (id: string) => void }) {
  if (!situation) return <Card title="本次调查的局势看板"><TextBlock text="本次调查没有保存局势看板。" /></Card>
  const { board, comparison } = situation
  const added = new Set(comparison.new_item_ids || [])
  const addedSources = new Set(comparison.new_source_ids || [])
  return <Card title="本次调查的局势看板"><Stack>
    <TextBlock text={`保存于 ${situation.received_at || "未记录"} · 地图 ${board.map?.id || "未记录"} · tick ${board.sample?.game_tick ?? "未记录"}`} />
    <TextBlock text="这是调查时保存的观察，查看不会读取当前游戏。" />
    {board.status !== "ok" ? <Alert tone="warning">本次看板读取不完整，请结合范围与缺失信息查看。</Alert> : null}
    <TextBlock text={comparison.available
      ? `相对观察 ${comparison.baseline_observation_id}，有稳定身份的事项新增 ${added.size} 项；无稳定身份的警报未参与比较。新增表示上次读取中未出现，不表示刚刚发生。`
      : `${reasons[comparison.reason || ""] || "没有可用的比较基准"}，未标记新增。`} />
    {comparison.available && comparison.compared_sections ? <TextBlock text={`比较栏目：${comparison.compared_sections.map(key => sections[key] || key).join("、")}。${comparison.excluded_sections?.length ? `未比较：${comparison.excluded_sections.map(key => sections[key] || key).join("、")}。` : ""}`} /> : null}
    {board.clock ? <details><summary>游戏时间</summary><JsonView data={board.clock} /></details> : null}
    {board.items.length ? board.items.map((item, index) => <Stack key={item.id || `item-${index}`}>
      <StatusBadge tone={item.id && added.has(item.id) ? "info" : "default"} label={`${item.id && added.has(item.id) ? "新增 · " : ""}${kinds[item.kind] || item.kind}`} />
      <TextBlock text={item.label || item.id || "未命名事项"} />
      {!(item.id && added.has(item.id)) && item.sources.some(source => source.id && addedSources.has(source.id)) ? <TextBlock text="本项新增了来源记录。" /> : null}
      {sourceFacts(item.sources) ? <TextBlock text={sourceFacts(item.sources)} /> : null}
      <details><summary>{`查看事实 · ${item.sources.length} 个来源`}</summary><JsonView data={item.sources} /></details>
    </Stack>) : <TextBlock text="本次读取范围内没有返回事项。" />}
    <details><summary>读取范围与缺失信息</summary><JsonView data={{ scope: board.scope, issues: board.issues, unavailable: Object.fromEntries(Object.entries(board).filter(([key]) => key.endsWith("_unavailable"))) }} /></details>
    <Button onClick={() => readEvidence(situation.observation_id)}>{`读取看板原始证据 ${situation.observation_id}`}</Button>
  </Stack></Card>
}
