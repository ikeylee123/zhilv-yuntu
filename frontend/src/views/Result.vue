<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { message } from "ant-design-vue";

import AmapTripMap from "../components/AmapTripMap.vue";
import {
  editTrip,
  fetchWeatherForecast,
  getMarkdownExportUrl,
  getPdfExportUrl,
  saveTrip,
} from "../services/api";
import type { Itinerary, WeatherForecastResponse } from "../types";

const props = defineProps<{
  itinerary: Itinerary | null;
}>();

const emit = defineEmits<{
  backHome: [];
  viewHistory: [];
  updated: [itinerary: Itinerary];
}>();

const saving = ref(false);
const exportingPdf = ref(false);
const exportingMarkdown = ref(false);
const editing = ref(false);
const editScope = ref("day_1");
const editInstruction = ref("这一天节奏更轻松一点，减少固定安排。");
const weatherLoading = ref(false);
const weatherError = ref("");
const weather = ref<WeatherForecastResponse | null>(null);
const failedImageKeys = ref(new Set<string>());

function formatShortDate(dateText?: string | null): string {
  if (!dateText) return "待定";
  const parts = dateText.split("-");
  return parts.length !== 3 ? dateText : `${parts[1]}-${parts[2]}`;
}

function formatWeatherDate(dateText?: string | null, week?: string | null): string {
  const weekdayMap: Record<string, string> = {
    "1": "周一", "2": "周二", "3": "周三", "4": "周四",
    "5": "周五", "6": "周六", "7": "周日",
  };
  const weekday = week ? weekdayMap[week] || `周${week}` : "";
  return [formatShortDate(dateText), weekday].filter(Boolean).join(" ");
}

const budgetItems = computed(() => {
  if (!props.itinerary) return [];
  const b = props.itinerary.budget_breakdown;
  return [
    { label: "景点门票", value: `¥${b.tickets.toFixed(0)}` },
    { label: "酒店住宿", value: `¥${b.hotel.toFixed(0)}` },
    { label: "餐饮费用", value: `¥${b.meals.toFixed(0)}` },
    { label: "交通费用", value: `¥${b.transport.toFixed(0)}` },
  ];
});

const dayBudgetItems = computed(() => {
  if (!props.itinerary) return [];
  return props.itinerary.days.map((day) => {
    const tickets = day.spots.reduce((s, sp) => s + (sp.estimated_cost ?? 0), 0);
    const meals = day.meals.reduce((s, m) => s + (m.estimated_cost ?? 0), 0);
    const transport = day.transport.reduce((s, t) => s + (t.estimated_cost ?? 0), 0);
    const hotel = day.hotel?.estimated_cost ?? 0;
    return { key: day.day_index, title: `第${day.day_index}天`, subtitle: day.theme || "", tickets, meals, transport, hotel, total: tickets + meals + transport + hotel };
  });
});

const mapPoints = computed(() => {
  if (!props.itinerary) return [];
  return props.itinerary.days.flatMap((day) =>
    day.spots.map((spot) => ({
      key: `${day.day_index}-${spot.name}`,
      dayIndex: day.day_index,
      date: day.date || "待定",
      theme: day.theme || "",
      name: spot.name,
      address: spot.address || spot.location || "待补充",
      latitude: spot.latitude,
      longitude: spot.longitude,
      poiId: spot.poi_id,
      imageUrl: spot.image_url,
      description: spot.description || "暂无说明",
    }))
  );
});

const technicalTipKeywords = ["LLM", "RAG", "LangChain", "Chroma", "演示", "测试", "规则", "模型", "源码"];
const rainWeatherKeywords = ["雨", "阵雨", "雷阵雨", "小雨", "中雨", "大雨"];
const sunnyTipKeywords = ["防晒", "太阳", "日照", "晒"];

const weatherText = computed(() => {
  if (!weather.value) return "";
  return weather.value.days.map((d) => `${d.day_weather || ""}${d.night_weather || ""}`).join(" ");
});

const hasRainyWeather = computed(() => rainWeatherKeywords.some((k) => weatherText.value.includes(k)));

const displayTips = computed(() => {
  if (!props.itinerary) return [];
  const tips = props.itinerary.tips.map((t) => t.trim()).filter(Boolean).filter((t) => !technicalTipKeywords.some((k) => t.includes(k)));
  const weatherAware = hasRainyWeather.value ? tips.filter((t) => !sunnyTipKeywords.some((k) => t.includes(k))) : tips;
  if (hasRainyWeather.value) {
    weatherAware.push("天气可能有雨，建议随身带伞或轻便雨衣。");
    weatherAware.push("阴雨天路面湿滑，建议穿防滑鞋。");
  }
  return Array.from(new Set(weatherAware));
});

function buildVisibleItinerary(): Itinerary | null {
  if (!props.itinerary) return null;
  return { ...props.itinerary, tips: displayTips.value };
}

function markImageAsFailed(pointKey: string) {
  failedImageKeys.value = new Set([...failedImageKeys.value, pointKey]);
}

async function loadWeather() {
  if (!props.itinerary?.destination) { weather.value = null; return; }
  weatherLoading.value = true;
  weatherError.value = "";
  try { weather.value = await fetchWeatherForecast(props.itinerary.destination); }
  catch { weather.value = null; weatherError.value = "天气信息加载失败。"; }
  finally { weatherLoading.value = false; }
}

watch(() => props.itinerary?.destination, () => { void loadWeather(); }, { immediate: true });
watch(() => props.itinerary?.trip_id, () => {
  const first = props.itinerary?.days[0];
  editScope.value = first ? `day_${first.day_index}` : "day_1";
}, { immediate: true });

async function openPdfExport() {
  const it = buildVisibleItinerary(); if (!it) return;
  const w = window.open("about:blank", "_blank");
  exportingPdf.value = true;
  try { await saveTrip(it); if (w) w.location.href = getPdfExportUrl(it.trip_id); }
  catch { w?.close(); message.error("导出 PDF 前同步当前行程失败。"); }
  finally { exportingPdf.value = false; }
}

async function openMarkdownExport() {
  const it = buildVisibleItinerary(); if (!it) return;
  const w = window.open("about:blank", "_blank");
  exportingMarkdown.value = true;
  try { await saveTrip(it); if (w) w.location.href = getMarkdownExportUrl(it.trip_id); }
  catch { w?.close(); message.error("导出 Markdown 前同步当前行程失败。"); }
  finally { exportingMarkdown.value = false; }
}

async function handleSave() {
  const it = buildVisibleItinerary(); if (!it) return;
  saving.value = true;
  try { await saveTrip(it); message.success("行程已保存。"); }
  catch { message.error("保存行程失败。"); }
  finally { saving.value = false; }
}

async function handleEdit() {
  if (!props.itinerary) return;
  if (props.itinerary.state_schema_version) {
    message.info("???????????????????????");
    return;
  }
  const instruction = editInstruction.value.trim();
  if (!instruction) { message.warning("请先输入想如何调整行程。"); return; }
  editing.value = true;
  try {
    const updated = await editTrip({ trip_id: props.itinerary.trip_id, current_itinerary: props.itinerary, user_instruction: instruction, edit_scope: editScope.value, preserve_constraints: ["保留预算结构", "保留目的地和旅行日期"] });
    emit("updated", updated);
    message.success("行程已智能调整。");
  } catch { message.error("智能调整失败。"); }
  finally { editing.value = false; }
}
</script>

<template>
  <section v-if="itinerary" class="result-page">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="sidebar__section">
        <div class="sidebar__label">操作</div>
        <button class="ios-btn ios-btn--text" @click="$emit('backHome')">← 返回规划</button>
        <button class="ios-btn ios-btn--text" :disabled="saving" @click="handleSave">{{ saving ? "保存中..." : "保存行程" }}</button>
        <button class="ios-btn ios-btn--text" @click="$emit('viewHistory')">历史列表</button>
      </div>
      <div class="sidebar__divider" />
      <div class="sidebar__section">
        <div class="sidebar__label">导出</div>
        <button class="ios-btn ios-btn--text" :disabled="exportingPdf" @click="openPdfExport">{{ exportingPdf ? "准备中..." : "导出 PDF" }}</button>
        <button class="ios-btn ios-btn--text" :disabled="exportingMarkdown" @click="openMarkdownExport">{{ exportingMarkdown ? "准备中..." : "导出 Markdown" }}</button>
      </div>
    </aside>

    <!-- 主内容 -->
    <div class="result-content">
      <!-- 行程概览 -->
      <div class="ios-card">
        <h2 class="ios-card__title">{{ itinerary.destination }}旅行计划</h2>
        <div class="ios-info"><span class="ios-info__label">行程 ID</span><span>{{ itinerary.trip_id }}</span></div>
        <div class="ios-info"><span class="ios-info__label">日期</span><span>{{ itinerary.days[0]?.date || "待定" }} 至 {{ itinerary.days[itinerary.days.length - 1]?.date || "待定" }}</span></div>
        <p class="ios-summary">{{ itinerary.summary }}</p>
        <div v-if="displayTips.length" class="ios-tips">
          <div class="ios-tips__title">旅行提示</div>
          <ul><li v-for="tip in displayTips" :key="tip">{{ tip }}</li></ul>
        </div>
      </div>

      <!-- 预算 -->
      <div class="ios-card">
        <div class="ios-card__header">预算明细</div>
        <div class="ios-budget-grid">
          <div v-for="item in budgetItems" :key="item.label" class="ios-budget-item">
            <span class="ios-budget-item__label">{{ item.label }}</span>
            <span class="ios-budget-item__value">{{ item.value }}</span>
          </div>
        </div>
        <div class="ios-budget-total">
          <span>预估总费用</span>
          <strong>¥{{ itinerary.estimated_budget.toFixed(0) }}</strong>
        </div>
      </div>

      <!-- 地图 -->
      <div class="ios-card ios-card--map">
        <div class="ios-card__header">景点地图</div>
        <AmapTripMap :points="mapPoints" />
      </div>

      <!-- 天气 -->
      <div class="ios-card">
        <div class="ios-card__header">天气信息</div>
        <div v-if="weatherLoading" class="ios-empty">正在加载...</div>
        <div v-else-if="weatherError" class="ios-empty">{{ weatherError }}</div>
        <div v-else-if="weather" class="ios-weather-grid">
          <div v-for="day in weather.days" :key="`${day.date}-${day.week}`" class="ios-weather-item">
            <div class="ios-weather-item__date">{{ formatWeatherDate(day.date, day.week) }}</div>
            <div class="ios-weather-item__temp">{{ day.day_temp || "-" }}° / {{ day.night_temp || "-" }}°</div>
            <div class="ios-weather-item__desc">{{ day.day_weather || "未知" }} / {{ day.night_weather || "未知" }}</div>
          </div>
        </div>
        <div v-else class="ios-empty">暂无天气信息</div>
      </div>

      <!-- 智能调整 -->
      <div class="ios-card ios-card--full">
        <div class="ios-card__header">智能调整行程</div>
        <div class="ios-edit-row">
          <select v-model="editScope" class="ios-select">
            <option v-for="day in itinerary.days" :key="day.day_index" :value="`day_${day.day_index}`">第{{ day.day_index }}天 · {{ day.theme || "" }}</option>
          </select>
          <button class="ios-btn ios-btn--primary ios-btn--sm" :disabled="editing" @click="handleEdit">{{ editing ? "调整中..." : "智能调整" }}</button>
        </div>
        <textarea v-model="editInstruction" class="ios-textarea" rows="2" placeholder="例如：第二天轻松一点，不要安排太满。" />
      </div>

      <!-- 按天花费 -->
      <div class="ios-card ios-card--full">
        <div class="ios-card__header">按天花费</div>
        <div class="ios-day-budget-grid">
          <div v-for="item in dayBudgetItems" :key="item.key" class="ios-day-budget">
            <div class="ios-day-budget__head"><span>{{ item.title }}</span><span>{{ item.subtitle }}</span></div>
            <div class="ios-day-budget__body">
              <div class="ios-row-between"><span>门票</span><span>¥{{ item.tickets.toFixed(0) }}</span></div>
              <div class="ios-row-between"><span>餐饮</span><span>¥{{ item.meals.toFixed(0) }}</span></div>
              <div class="ios-row-between"><span>交通</span><span>¥{{ item.transport.toFixed(0) }}</span></div>
              <div class="ios-row-between"><span>住宿</span><span>¥{{ item.hotel.toFixed(0) }}</span></div>
              <div class="ios-row-between ios-row-between--bold"><span>当日合计</span><span>¥{{ item.total.toFixed(0) }}</span></div>
            </div>
          </div>
        </div>
      </div>

      <!-- 地图点位明细 -->
      <div class="ios-card ios-card--full">
        <div class="ios-card__header">地图点位明细</div>
        <div class="ios-point-grid">
          <div v-for="point in mapPoints" :key="point.key" class="ios-point">
            <div class="ios-point__head"><span>第{{ point.dayIndex }}天 · {{ point.name }}</span><span>{{ formatShortDate(point.date) }}</span></div>
            <img
              v-if="point.imageUrl && !failedImageKeys.has(point.key)"
              class="ios-point__img"
              :src="point.imageUrl"
              :alt="`${point.name} 图片`"
              @error="markImageAsFailed(point.key)"
            />
            <div v-else class="ios-point__img ios-point__img--empty">暂无图片</div>
            <div class="ios-point__info"><span class="ios-muted">主题：</span>{{ point.theme }}</div>
            <div class="ios-point__info"><span class="ios-muted">地址：</span>{{ point.address }}</div>
            <div class="ios-point__desc">{{ point.description }}</div>
          </div>
        </div>
      </div>

      <!-- 每日行程 -->
      <div class="ios-card ios-card--full">
        <div class="ios-card__header">每日行程</div>
        <div class="ios-day-list">
          <details v-for="day in itinerary.days" :key="day.day_index" class="ios-day" :open="day.day_index === 1">
            <summary class="ios-day__head">
              <span>第{{ day.day_index }}天 · {{ day.theme || "" }}</span>
              <span class="ios-muted">{{ formatShortDate(day.date) }}</span>
            </summary>
            <div class="ios-day__body">
              <div><span class="ios-muted">主要景点：</span>{{ day.spots[0]?.name || "未安排" }}</div>
              <div><span class="ios-muted">景点地址：</span>{{ day.spots[0]?.address || day.spots[0]?.location || "待补充" }}</div>
              <div><span class="ios-muted">餐饮建议：</span>{{ day.meals[0]?.name || "未安排" }}</div>
              <div><span class="ios-muted">餐饮地址：</span>{{ day.meals[0]?.address || "待补充" }}</div>
              <div><span class="ios-muted">住宿安排：</span>{{ day.hotel?.name || "未安排" }}</div>
              <div><span class="ios-muted">住宿地址：</span>{{ day.hotel?.address || day.hotel?.location || "待补充" }}</div>
              <div><span class="ios-muted">交通信息：</span>{{ day.transport[0]?.distance_km != null ? `${day.transport[0].distance_km.toFixed(2)} km / ${day.transport[0].estimated_minutes ?? 0} 分钟` : day.transport[0]?.duration || "待补充" }}</div>
              <div><span class="ios-muted">备注：</span>{{ day.notes[day.notes.length - 1] || "无" }}</div>
            </div>
          </details>
        </div>
      </div>
    </div>
  </section>

  <section v-else class="empty-state">
    <div class="ios-card" style="text-align:center; max-width:480px; margin:80px auto;">
      <h2 style="margin:0 0 12px;">还没有生成结果</h2>
      <p class="ios-muted" style="margin:0 0 20px;">先回到规划页生成一条行程。</p>
      <button class="ios-btn ios-btn--primary" @click="$emit('backHome')">返回规划页</button>
    </div>
  </section>
</template>

<style scoped>
.result-page {
  display: grid;
  grid-template-columns: 180px 1fr;
  gap: 16px;
}

/* 侧边栏 */
.sidebar {
  align-self: start;
  position: sticky;
  top: 76px;
  padding: 16px;
  border-radius: 12px;
  background: #FFFFFF;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.sidebar__section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sidebar__label {
  font-size: 12px;
  font-weight: 600;
  color: #8E8E93;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 4px 8px;
  margin-bottom: 4px;
}

.sidebar__divider {
  height: 0.5px;
  background: rgba(0, 0, 0, 0.06);
  margin: 8px 0;
}

/* iOS 按钮 */
.ios-btn {
  border: none;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
  text-align: left;
}

.ios-btn:active { transform: scale(0.97); }
.ios-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.ios-btn--text {
  background: transparent;
  color: #007AFF;
}

.ios-btn--primary {
  background: #007AFF;
  color: #FFFFFF;
}

.ios-btn--sm {
  padding: 8px 16px;
  font-size: 13px;
}

/* 主内容 */
.result-content {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

/* iOS 卡片 */
.ios-card {
  padding: 20px;
  border-radius: 12px;
  background: #FFFFFF;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.ios-card--full { grid-column: 1 / -1; }
.ios-card--map { min-height: 320px; }

.ios-card__title {
  margin: 0 0 16px;
  font-size: 22px;
  font-weight: 700;
  color: #1C1C1E;
}

.ios-card__header {
  font-size: 15px;
  font-weight: 600;
  color: #1C1C1E;
  margin-bottom: 14px;
  padding-bottom: 10px;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.06);
}

/* 信息行 */
.ios-info {
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.04);
  font-size: 14px;
  color: #3C3C43;
}

.ios-info__label { color: #8E8E93; }

.ios-summary {
  margin: 14px 0 0;
  font-size: 14px;
  line-height: 1.7;
  color: #3C3C43;
}

.ios-muted { color: #8E8E93; font-size: 13px; }

/* 旅行提示 */
.ios-tips {
  margin-top: 16px;
  padding: 14px 16px;
  border-radius: 10px;
  background: #F2F2F7;
}

.ios-tips__title { font-size: 13px; font-weight: 600; color: #3C3C43; margin-bottom: 8px; }
.ios-tips ul { margin: 0; padding-left: 18px; font-size: 13px; color: #636366; line-height: 1.8; }

/* 预算 */
.ios-budget-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.ios-budget-item {
  display: flex;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 8px;
  background: #F2F2F7;
}

.ios-budget-item__label { font-size: 13px; color: #8E8E93; }
.ios-budget-item__value { font-size: 15px; font-weight: 600; color: #007AFF; }

.ios-budget-total {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 12px;
  padding: 14px 16px;
  border-radius: 10px;
  background: #007AFF;
  color: #FFFFFF;
  font-size: 15px;
}

.ios-budget-total strong { font-size: 22px; }

/* 天气 */
.ios-weather-grid { display: grid; gap: 8px; }

.ios-weather-item {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 12px;
  border-radius: 8px;
  background: #F2F2F7;
}

.ios-weather-item__date { font-size: 13px; font-weight: 600; color: #3C3C43; min-width: 60px; }
.ios-weather-item__temp { font-size: 18px; font-weight: 700; color: #007AFF; min-width: 80px; }
.ios-weather-item__desc { font-size: 13px; color: #636366; }

.ios-empty { font-size: 14px; color: #8E8E93; }

/* 智能调整 */
.ios-edit-row {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
}

.ios-select {
  flex: 1;
  height: 36px;
  padding: 0 12px;
  border: 1px solid #D1D1D6;
  border-radius: 8px;
  font-size: 14px;
  color: #1C1C1E;
  background: #FFFFFF;
  outline: none;
}

.ios-select:focus { border-color: #007AFF; }

.ios-textarea {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #D1D1D6;
  border-radius: 8px;
  font-size: 14px;
  color: #1C1C1E;
  background: #FFFFFF;
  outline: none;
  resize: vertical;
  font-family: inherit;
}

.ios-textarea:focus { border-color: #007AFF; }

/* 按天花费 */
.ios-day-budget-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}

.ios-day-budget {
  border-radius: 10px;
  overflow: hidden;
  border: 0.5px solid rgba(0, 0, 0, 0.06);
}

.ios-day-budget__head {
  display: flex;
  justify-content: space-between;
  padding: 10px 12px;
  background: #F2F2F7;
  font-size: 13px;
  font-weight: 600;
  color: #3C3C43;
}

.ios-day-budget__body {
  display: grid;
  gap: 6px;
  padding: 12px;
}

.ios-row-between {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: #636366;
}

.ios-row-between--bold {
  padding-top: 8px;
  border-top: 0.5px solid rgba(0, 0, 0, 0.06);
  font-weight: 600;
  color: #1C1C1E;
}

/* 点位明细 */
.ios-point-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 10px;
}

.ios-point {
  border-radius: 10px;
  overflow: hidden;
  border: 0.5px solid rgba(0, 0, 0, 0.06);
}

.ios-point__head {
  display: flex;
  justify-content: space-between;
  padding: 10px 12px;
  background: #F2F2F7;
  font-size: 13px;
  font-weight: 600;
  color: #3C3C43;
}

.ios-point__img {
  display: block;
  width: 100%;
  height: 140px;
  background-color: #F2F2F7;
  object-fit: cover;
}

.ios-point__img--empty {
  display: grid;
  place-items: center;
  font-size: 13px;
  color: #8E8E93;
}

.ios-point__info { padding: 6px 12px 0; font-size: 13px; color: #3C3C43; }
.ios-point__desc { padding: 8px 12px 12px; font-size: 13px; color: #636366; line-height: 1.6; }

/* 每日行程 */
.ios-day-list { display: grid; gap: 8px; }

.ios-day {
  border-radius: 10px;
  border: 0.5px solid rgba(0, 0, 0, 0.06);
  overflow: hidden;
}

.ios-day__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 14px;
  background: #F2F2F7;
  font-size: 14px;
  font-weight: 600;
  color: #1C1C1E;
  cursor: pointer;
  list-style: none;
}

.ios-day__head::-webkit-details-marker { display: none; }

.ios-day__head::after {
  content: "▸";
  font-size: 14px;
  color: #8E8E93;
  transition: transform 0.2s ease;
}

.ios-day[open] .ios-day__head::after {
  transform: rotate(90deg);
}

.ios-day__body {
  display: grid;
  gap: 8px;
  padding: 14px;
  font-size: 14px;
  color: #3C3C43;
  line-height: 1.7;
  border-top: 0.5px solid rgba(0, 0, 0, 0.06);
}

/* 空状态 */
.empty-state { min-height: 400px; }

@media (max-width: 960px) {
  .result-page { grid-template-columns: 1fr; }
  .sidebar { position: static; display: flex; gap: 16px; flex-wrap: wrap; }
  .sidebar__section { flex-direction: row; flex-wrap: wrap; gap: 8px; }
  .sidebar__divider { display: none; }
  .sidebar__label { display: none; }
  .result-content { grid-template-columns: 1fr; }
}
</style>
