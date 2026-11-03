<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t("restocking.title") }}</h2>
      <p>{{ t("restocking.description") }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t("common.loading") }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <div class="card budget-card">
        <div class="budget-row">
          <div class="budget-label">{{ t("restocking.budget") }}</div>
          <div class="budget-value">{{ money(budget) }}</div>
        </div>
        <input
          v-model.number="budget"
          type="range"
          class="budget-slider"
          min="0"
          :max="maxBudget"
          :step="BUDGET_STEP"
          :aria-label="t('restocking.budget')"
        />
      </div>

      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t("restocking.budget") }}</div>
          <div class="stat-value">{{ money(budget) }}</div>
        </div>
        <div class="stat-card warning">
          <div class="stat-label">{{ t("restocking.plannedSpend") }}</div>
          <div class="stat-value">{{ money(plannedSpend) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t("restocking.remaining") }}</div>
          <div class="stat-value">{{ money(remainingBudget) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">{{ t("restocking.maxLeadTime") }}</div>
          <div class="stat-value">
            {{
              maxLeadTime === null
                ? "-"
                : t("restocking.days", { count: maxLeadTime })
            }}
          </div>
        </div>
      </div>

      <div v-if="orderResult" class="success-message">
        {{ t("restocking.success", { orderNumber: orderResult.order_number }) }}
        <router-link to="/orders">{{ t("restocking.viewOrders") }}</router-link>
      </div>
      <div v-if="submitError" class="error">
        {{ t("restocking.error")
        }}<span v-if="submitDetail"> {{ submitDetail }}</span>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">
            {{ t("restocking.recommendations") }} ({{ selectedLines.length }})
          </h3>
          <button
            type="button"
            class="place-order-btn"
            :disabled="
              selectedLines.length === 0 || submitting || !!orderResult
            "
            @click="placeOrder"
          >
            {{
              submitting ? t("restocking.placing") : t("restocking.placeOrder")
            }}
          </button>
        </div>
        <div v-if="selectedLines.length === 0" class="empty-state">
          {{ t("restocking.noRecommendations") }}
        </div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t("restocking.table.sku") }}</th>
                <th>{{ t("restocking.table.itemName") }}</th>
                <th>{{ t("restocking.table.trend") }}</th>
                <th>{{ t("restocking.table.quantity") }}</th>
                <th>{{ t("restocking.table.unitCost") }}</th>
                <th>{{ t("restocking.table.lineTotal") }}</th>
                <th>{{ t("restocking.table.leadTime") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="line in selectedLines" :key="line.item_sku">
                <td>
                  <strong>{{ line.item_sku }}</strong>
                </td>
                <td>{{ translateProductName(line.item_name) }}</td>
                <td>
                  <span :class="['badge', line.trend]">{{
                    t(`trends.${line.trend}`)
                  }}</span>
                </td>
                <td>
                  {{ line.quantity }}
                  <span
                    v-if="line.partial"
                    class="badge warning partial-badge"
                    >{{ t("restocking.partial") }}</span
                  >
                </td>
                <td>{{ money(line.unit_cost, 2) }}</td>
                <td>
                  <strong>{{ money(line.lineTotal) }}</strong>
                </td>
                <td>
                  {{ t("restocking.days", { count: line.lead_time_days }) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted } from "vue";
import { api } from "../api";
import { useI18n } from "../composables/useI18n";
import { formatCurrency, formatCurrencyWithDecimals } from "../utils/currency";

const BUDGET_STEP = 500;

export default {
  name: "Restocking",
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n();
    const loading = ref(true);
    const error = ref(null);
    const forecasts = ref([]);
    const budget = ref(0); // always USD; converted only for display

    const submitting = ref(false);
    const submitError = ref(false);
    const submitDetail = ref("");
    const orderResult = ref(null);

    const money = (amount, decimals = 0) =>
      decimals
        ? formatCurrencyWithDecimals(amount, currentCurrency.value, decimals)
        : formatCurrency(amount, currentCurrency.value);

    // Money math is done in integer cents so the client's budget check matches the
    // server's (which rounds unit_cost and budget to cents) and avoids float drift.
    const toCents = (amount) => Math.round(amount * 100);

    // Full restock candidates, ranked by priority.
    // - increasing: stock up to the full forecasted demand
    // - stable/decreasing: only cover the shortfall (forecast - current), never negative
    // Ranking: increasing trend first, then highest full restock cost first so the
    // budget goes to the biggest-impact items.
    const candidates = computed(() => {
      return (
        forecasts.value
          .map((f) => {
            const quantity =
              f.trend === "increasing"
                ? f.forecasted_demand
                : Math.max(f.forecasted_demand - f.current_demand, 0);
            const unitCents = toCents(f.unit_cost);
            return {
              ...f,
              quantity,
              unitCents,
              fullCents: quantity * unitCents,
            };
          })
          // unitCents <= 0 would divide by zero when sizing a partial line
          .filter((c) => c.quantity > 0 && c.unitCents > 0)
          .sort((a, b) => {
            const aInc = a.trend === "increasing" ? 1 : 0;
            const bInc = b.trend === "increasing" ? 1 : 0;
            if (aInc !== bInc) return bInc - aInc;
            return b.fullCents - a.fullCents;
          })
      );
    });

    // Slider max: total cost of all candidates rounded up to the next 1000
    const maxBudget = computed(() => {
      const totalCents = candidates.value.reduce(
        (sum, c) => sum + c.fullCents,
        0,
      );
      return Math.ceil(totalCents / 100 / 1000) * 1000;
    });

    // Greedy fill in rank order. The first line that doesn't fit is trimmed to the
    // units we can still afford (if at least 1), then we stop: we don't skip ahead
    // to cheaper items, so higher-priority items are never displaced by lower ones.
    const selectedLines = computed(() => {
      const lines = [];
      let remainingCents = toCents(budget.value);
      for (const c of candidates.value) {
        if (c.fullCents <= remainingCents) {
          lines.push({
            ...c,
            lineCents: c.fullCents,
            lineTotal: c.fullCents / 100,
            partial: false,
          });
          remainingCents -= c.fullCents;
          continue;
        }
        const affordable = Math.floor(remainingCents / c.unitCents);
        if (affordable >= 1) {
          const lineCents = affordable * c.unitCents;
          lines.push({
            ...c,
            quantity: affordable,
            lineCents,
            lineTotal: lineCents / 100,
            partial: true,
          });
        }
        break;
      }
      return lines;
    });

    const plannedSpendCents = computed(() =>
      selectedLines.value.reduce((sum, l) => sum + l.lineCents, 0),
    );
    const plannedSpend = computed(() => plannedSpendCents.value / 100);
    const remainingBudget = computed(
      () => (toCents(budget.value) - plannedSpendCents.value) / 100,
    );
    const maxLeadTime = computed(() => {
      if (selectedLines.value.length === 0) return null;
      return Math.max(...selectedLines.value.map((l) => l.lead_time_days));
    });

    // A changed budget means a new plan: drop the old result/error so a stale banner
    // isn't shown and the button re-enables (it stays disabled after a success to
    // prevent accidentally submitting the same order twice).
    watch(budget, () => {
      orderResult.value = null;
      submitError.value = false;
      submitDetail.value = "";
    });

    const loadForecasts = async () => {
      try {
        loading.value = true;
        error.value = null;
        forecasts.value = await api.getDemandForecasts();
        // Set once after load; start at half the max, snapped to the slider step
        budget.value =
          Math.round(maxBudget.value / 2 / BUDGET_STEP) * BUDGET_STEP;
      } catch (err) {
        error.value = "Failed to load demand forecasts: " + err.message;
      } finally {
        loading.value = false;
      }
    };

    const placeOrder = async () => {
      if (submitting.value || selectedLines.value.length === 0) return;
      submitting.value = true;
      submitError.value = false;
      submitDetail.value = "";
      orderResult.value = null;
      try {
        orderResult.value = await api.createRestockOrder({
          budget: budget.value,
          items: selectedLines.value.map((l) => ({
            sku: l.item_sku,
            quantity: l.quantity,
          })),
        });
      } catch (err) {
        submitError.value = true;
        // 400 returns a string detail; 422 returns an array, which isn't user-friendly
        const detail = err.response?.data?.detail;
        submitDetail.value = typeof detail === "string" ? detail : "";
      } finally {
        submitting.value = false;
      }
    };

    onMounted(loadForecasts);

    return {
      t,
      translateProductName,
      BUDGET_STEP,
      loading,
      error,
      budget,
      maxBudget,
      selectedLines,
      plannedSpend,
      remainingBudget,
      maxLeadTime,
      submitting,
      submitError,
      submitDetail,
      orderResult,
      money,
      placeOrder,
    };
  },
};
</script>

<style scoped>
.budget-card {
  padding: 1.25rem;
  margin-bottom: 1.5rem;
}

.budget-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 1rem;
}

.budget-label {
  color: #64748b;
  font-size: 0.875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.budget-value {
  font-size: 2.25rem;
  font-weight: 700;
  color: #0f172a;
  letter-spacing: -0.025em;
}

.budget-slider {
  width: 100%;
  accent-color: #3b82f6;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.5rem 1rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}

.place-order-btn:hover:not(:disabled) {
  background: #1e293b;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.partial-badge {
  margin-left: 0.5rem;
}

.empty-state {
  padding: 2rem;
  text-align: center;
  color: #64748b;
  font-size: 0.938rem;
}

.success-message {
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
  padding: 1rem;
  border-radius: 8px;
  margin: 0 0 1.5rem;
  font-size: 0.938rem;
}

.success-message a {
  margin-left: 0.5rem;
  color: #166534;
  font-weight: 600;
}
</style>
