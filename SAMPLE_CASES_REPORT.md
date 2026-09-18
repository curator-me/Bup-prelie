# BUP CSE Fest 2026 Preliminary — Sample Cases Output Difference Report

> Generated automatically by `test_cases.py` on 2026-09-18 22:18:31
> **Test Source:** `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` | **Execution Mode:** `llm`

## 1. Executive Summary & Scorecard

| Metric | Count | Ratio | Description |
| :--- | :---: | :---: | :--- |
| **Total Cases Evaluated** | `10` | 100.0% | Public sample scenarios |
| **Official Pass Rate** | **`10 / 10`** | **`100.0%`** | Within 1.0% cost tolerance + all constraints met |
| — *Identical Schedules* | `3` | `30.0%` | Exact match on hourly dispatch & metrics |
| — *Equivalent Optimal* | `7` | `70.0%` | Equal optimal cost, alternate valid battery schedule |
| **Failing Cases** | `0` | `0.0%` | Directive mismatch or tolerance violation |

### Scorecard Overview

| Case ID | Label | Directives | Expected Cost (BDT) | Actual Cost (BDT) | Cost Diff | Peak Grid (Exp/Act) | Constraints | Verdict |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `SAMPLE-01` | Solar cleaning + distractor | ✅ Match | 38,365.00 | 38,365.00 | 0.00 (0.0%) | 175 / 187.5 | ✅ Valid | **EQUIV OPTIMAL** |
| `SAMPLE-02` | Battery charging maintenance | ✅ Match | 42,885.00 | 42,885.00 | 0.00 (0.0%) | 180 / 180 | ✅ Valid | **EQUIV OPTIMAL** |
| `SAMPLE-03` | Emergency reserve as percentage | ✅ Match | 35,480.00 | 35,480.00 | 0.00 (0.0%) | 205 / 205 | ✅ Valid | **EQUIV OPTIMAL** |
| `SAMPLE-04` | No-discharge protection test | ✅ Match | 40,495.00 | 40,495.00 | 0.00 (0.0%) | 225 / 225 | ✅ Valid | **EQUIV OPTIMAL** |
| `SAMPLE-05` | Temporary feeder grid cap | ✅ Match | 33,950.00 | 33,950.00 | 0.00 (0.0%) | 175 / 175 | ✅ Valid | **EQUIV OPTIMAL** |
| `SAMPLE-06` | Multiple notes with distractor | ✅ Match | 34,090.00 | 34,090.00 | 0.00 (0.0%) | 175 / 175 | ✅ Valid | **PASS** |
| `SAMPLE-07` | Reserve plus transformer cap | ✅ Match | 38,550.00 | 38,550.00 | 0.00 (0.0%) | 185 / 185 | ✅ Valid | **PASS** |
| `SAMPLE-08` | Separate charge/discharge outages | ✅ Match | 37,665.00 | 37,665.00 | 0.00 (0.0%) | 210 / 210 | ✅ Valid | **PASS** |
| `SAMPLE-09` | Reduction wording normalization | ✅ Match | 34,873.00 | 34,873.00 | 0.00 (0.0%) | 170 / 187 | ✅ Valid | **EQUIV OPTIMAL** |
| `SAMPLE-10` | Multi-constraint evening operation | ✅ Match | 41,620.00 | 41,620.00 | 0.00 (0.0%) | 190 / 190 | ✅ Valid | **EQUIV OPTIMAL** |

## 2. Schedule Equivalence in GridWise Optimization

> [!NOTE]
> As specified in the official competition guide:
> *"The expected_output for each case is one valid optimal reference result. Another schedule may also be accepted if it satisfies the same directive ground truth and all GridWise constraints and achieves equivalent optimal cost within the official tolerance (1.0%)."*

Linear Programming (LP) problems frequently have **degenerate optimal solutions** (multiple extreme points with the exact same objective value). In GridWise:
1. When adjacent hours share the same tariff tier (e.g. standard tariff hours), the solver can shift battery charging or discharging between those hours without changing the total cost.
2. Secondary objectives (like peak shaving) may lead solvers to different optimal charge/discharge distributions unless explicitly constrained.

## 3. Detailed Case-by-Case Difference Analysis

### Case `SAMPLE-01`: Solar cleaning + distractor

- **Execution Latency:** `2749.1 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast."*
  - `[1]` *"The sports office moved next month's registration deadline."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `solar_reduction` (applies=True) | `solar_reduction` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `1` | `no_op` (applies=False) | `no_op` (applies=False) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `38,365.00` | `38,365.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,692.50` | `2,692.50` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `175.00` | `187.50` | `+12.50 kWh` | +12.50 |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 3 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H13` | `grid_kwh` | `152.5` | `187.5` | `+35.00` |
| `H13` | `battery_kwh` | `15.0` | `50.0` | `+35.00` |
| `H13` | `battery_energy_after_kwh` | `120.0` | `155.0` | `+35.00` |
| `H14` | `battery_energy_after_kwh` | `170.0` | `205.0` | `+35.00` |
| `H15` | `grid_kwh` | `125.0` | `90.0` | `-35.00` |
| `H15` | `battery_kwh` | `50.0` | `15.0` | `-35.00` |

---

### Case `SAMPLE-02`: Battery charging maintenance

- **Execution Latency:** `1828.3 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"The battery charger will be isolated from 2 AM until 5 AM for electrical maintenance."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `no_charge_window` (applies=True) | `no_charge_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `42,885.00` | `42,885.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,915.00` | `2,915.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `180.00` | `180.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 3 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H13` | `grid_kwh` | `70.0` | `125.0` | `+55.00` |
| `H13` | `battery_action` | `idle` | `charge` | `-` |
| `H13` | `battery_kwh` | `0.0` | `55.0` | `+55.00` |
| `H13` | `battery_energy_after_kwh` | `90.0` | `145.0` | `+55.00` |
| `H14` | `battery_energy_after_kwh` | `145.0` | `200.0` | `+55.00` |
| `H15` | `grid_kwh` | `155.0` | `100.0` | `-55.00` |
| `H15` | `battery_action` | `charge` | `idle` | `-` |
| `H15` | `battery_kwh` | `55.0` | `0.0` | `-55.00` |

---

### Case `SAMPLE-03`: Emergency reserve as percentage

- **Execution Latency:** `1700.5 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM for emergency operations."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `minimum_battery_reserve` (applies=True) | `minimum_battery_reserve` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `35,480.00` | `35,480.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,430.00` | `2,430.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `205.00` | `205.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 3 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H13` | `grid_kwh` | `10.0` | `30.0` | `+20.00` |
| `H13` | `battery_action` | `idle` | `charge` | `-` |
| `H13` | `battery_kwh` | `0.0` | `20.0` | `+20.00` |
| `H13` | `battery_energy_after_kwh` | `130.0` | `150.0` | `+20.00` |
| `H14` | `battery_energy_after_kwh` | `180.0` | `200.0` | `+20.00` |
| `H15` | `grid_kwh` | `95.0` | `75.0` | `-20.00` |
| `H15` | `battery_action` | `charge` | `idle` | `-` |
| `H15` | `battery_kwh` | `20.0` | `0.0` | `-20.00` |

---

### Case `SAMPLE-04`: No-discharge protection test

- **Execution Latency:** `1639.3 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"For protection testing, the battery must not discharge from 6 PM until 8 PM."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `no_discharge_window` (applies=True) | `no_discharge_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `40,495.00` | `40,495.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,645.00` | `2,645.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `225.00` | `225.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 7 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H02` | `grid_kwh` | `85.0` | `130.0` | `+45.00` |
| `H02` | `battery_action` | `idle` | `charge` | `-` |
| `H02` | `battery_kwh` | `0.0` | `45.0` | `+45.00` |
| `H02` | `battery_energy_after_kwh` | `75.0` | `120.0` | `+45.00` |
| `H03` | `battery_energy_after_kwh` | `130.0` | `175.0` | `+45.00` |
| `H04` | `battery_energy_after_kwh` | `185.0` | `230.0` | `+45.00` |
| `H05` | `grid_kwh` | `145.0` | `100.0` | `-45.00` |
| `H05` | `battery_action` | `charge` | `idle` | `-` |
| `H05` | `battery_kwh` | `45.0` | `0.0` | `-45.00` |
| `H13` | `grid_kwh` | `20.0` | `65.0` | `+45.00` |
| `H13` | `battery_action` | `idle` | `charge` | `-` |
| `H13` | `battery_kwh` | `0.0` | `45.0` | `+45.00` |
| `H13` | `battery_energy_after_kwh` | `130.0` | `175.0` | `+45.00` |
| `H14` | `battery_energy_after_kwh` | `185.0` | `230.0` | `+45.00` |
| `H15` | `grid_kwh` | `130.0` | `85.0` | `-45.00` |
| ... | *and 2 more field variations* | | | |

---

### Case `SAMPLE-05`: Temporary feeder grid cap

- **Execution Latency:** `2148.0 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour because the feeder is operating under a temporary limit."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `max_grid_window` (applies=True) | `max_grid_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `33,950.00` | `33,950.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,430.00` | `2,430.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `175.00` | `175.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 3 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H13` | `grid_kwh` | `20.0` | `10.0` | `-10.00` |
| `H13` | `battery_action` | `charge` | `idle` | `-` |
| `H13` | `battery_kwh` | `10.0` | `0.0` | `-10.00` |
| `H13` | `battery_energy_after_kwh` | `180.0` | `170.0` | `-10.00` |
| `H14` | `battery_energy_after_kwh` | `240.0` | `230.0` | `-10.00` |
| `H15` | `grid_kwh` | `75.0` | `85.0` | `+10.00` |
| `H15` | `battery_action` | `idle` | `charge` | `-` |
| `H15` | `battery_kwh` | `0.0` | `10.0` | `+10.00` |

---

### Case `SAMPLE-06`: Multiple notes with distractor

- **Execution Latency:** `2959.5 ms` | **HTTP Status:** `200` | **Overall Verdict:** `PASS`
- **Operator Notes:**
  - `[0]` *"Cloud cover during panel inspection will leave about half of the forecast solar output from 10 AM until noon."*
  - `[1]` *"The charging circuit will be unavailable from 2 PM until 4 PM."*
  - `[2]` *"The library is extending book-return hours next week."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `solar_reduction` (applies=True) | `solar_reduction` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `1` | `no_charge_window` (applies=True) | `no_charge_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `2` | `no_op` (applies=False) | `no_op` (applies=False) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `34,090.00` | `34,090.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,395.00` | `2,395.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `175.00` | `175.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

✅ **All 24 hours match the reference schedule byte-for-byte.**

---

### Case `SAMPLE-07`: Reserve plus transformer cap

- **Execution Latency:** `2390.2 ms` | **HTTP Status:** `200` | **Overall Verdict:** `PASS`
- **Operator Notes:**
  - `[0]` *"Keep at least 90 kWh in the battery from 6 PM until 10 PM for emergency services."*
  - `[1]` *"The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `minimum_battery_reserve` (applies=True) | `minimum_battery_reserve` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `1` | `max_grid_window` (applies=True) | `max_grid_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `38,550.00` | `38,550.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,560.00` | `2,560.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `185.00` | `185.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

✅ **All 24 hours match the reference schedule byte-for-byte.**

---

### Case `SAMPLE-08`: Separate charge/discharge outages

- **Execution Latency:** `283.9 ms` | **HTTP Status:** `200` | **Overall Verdict:** `PASS`
- **Operator Notes:**
  - `[0]` *"Battery charging is disabled from 11 AM until 1 PM while technicians inspect the charger."*
  - `[1]` *"Do not discharge the battery from 5 PM until 7 PM during relay testing."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `no_charge_window` (applies=True) | `no_charge_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `1` | `no_discharge_window` (applies=True) | `no_discharge_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `37,665.00` | `37,665.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,490.00` | `2,490.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `210.00` | `210.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

✅ **All 24 hours match the reference schedule byte-for-byte.**

---

### Case `SAMPLE-09`: Reduction wording normalization

- **Execution Latency:** `2414.1 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"Expect an 80% reduction in rooftop solar between 11 AM and 2 PM because of inverter work."*
  - `[1]` *"The student affairs office will publish club notices tomorrow."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `solar_reduction` (applies=True) | `solar_reduction` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `1` | `no_op` (applies=False) | `no_op` (applies=False) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `34,873.00` | `34,873.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,504.00` | `2,504.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `170.00` | `187.00` | `+17.00 kWh` | +17.00 |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 3 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H13` | `grid_kwh` | `127.0` | `187.0` | `+60.00` |
| `H13` | `battery_action` | `idle` | `charge` | `-` |
| `H13` | `battery_kwh` | `0.0` | `60.0` | `+60.00` |
| `H13` | `battery_energy_after_kwh` | `120.0` | `180.0` | `+60.00` |
| `H14` | `battery_energy_after_kwh` | `180.0` | `240.0` | `+60.00` |
| `H15` | `grid_kwh` | `100.0` | `40.0` | `-60.00` |
| `H15` | `battery_action` | `charge` | `idle` | `-` |
| `H15` | `battery_kwh` | `60.0` | `0.0` | `-60.00` |

---

### Case `SAMPLE-10`: Multi-constraint evening operation

- **Execution Latency:** `2637.0 ms` | **HTTP Status:** `200` | **Overall Verdict:** `EQUIVALENT_OPTIMAL`
- **Operator Notes:**
  - `[0]` *"The data center requires at least 80 kWh to remain in the battery from 6 PM until 10 PM."*
  - `[1]` *"Grid intake must stay at or below 190 kWh from 7 PM until 10 PM while the substation is constrained."*
  - `[2]` *"A seminar room booking was moved to next week."*

#### Directive Interpretations

| Note | Expected Directive | Actual Directive | Match Status | Notes / Adjustments |
| :---: | :--- | :--- | :---: | :--- |
| `0` | `minimum_battery_reserve` (applies=True) | `minimum_battery_reserve` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `1` | `max_grid_window` (applies=True) | `max_grid_window` (applies=True) | ✅ Exact Match | Fully aligned with ground truth |
| `2` | `no_op` (applies=False) | `no_op` (applies=False) | ✅ Exact Match | Fully aligned with ground truth |

#### Macro Output Metrics

| Metric | Expected Reference | Actual Output | Difference | Within Tolerance? |
| :--- | :---: | :---: | :---: | :---: |
| **Total Cost (BDT)** | `41,620.00` | `41,620.00` | `+0.00 BDT (+0.00%)` | ✅ Yes (<= 1.0%) |
| **Total Grid (kWh)** | `2,715.00` | `2,715.00` | `+0.00 kWh` | ✅ Identical |
| **Peak Grid (kWh)** | `190.00` | `190.00` | `+0.00 kWh` | ✅ Identical |

#### Physical Constraints Validation

- ✅ **Energy Balance:** Conserved across all 24 hours ($P_{\text{grid}} + P_{\text{solar}} + P_{\text{discharge}} = P_{\text{demand}} + P_{\text{charge}}$)
- ✅ **Solar Availability:** Usable solar strictly within calculated effective limits
- ✅ **Battery Reserve & Bounds:** Energy state strictly bounded between reserve floor and maximum capacity
- ✅ **Inverter Rate Limits:** Maximum hourly charge and discharge rates respected
- ✅ **Directive Windows:** Charging/discharging lockouts and grid caps enforced
- ✅ **End-of-day Neutrality:** Battery energy returned to initial storage level at hour 23

#### Hourly Plan Schedule Comparison

ℹ️ **Schedule variation observed in 5 of 24 hours.** (Total cost is identical, proving schedule equivalence).

| Hour | Variable | Expected Reference | Actual Solution | Delta |
| :---: | :--- | :---: | :---: | :---: |
| `H01` | `grid_kwh` | `155.0` | `100.0` | `-55.00` |
| `H01` | `battery_action` | `charge` | `idle` | `-` |
| `H01` | `battery_kwh` | `55.0` | `0.0` | `-55.00` |
| `H01` | `battery_energy_after_kwh` | `130.0` | `75.0` | `-55.00` |
| `H02` | `grid_kwh` | `95.0` | `150.0` | `+55.00` |
| `H02` | `battery_action` | `idle` | `charge` | `-` |
| `H02` | `battery_kwh` | `0.0` | `55.0` | `+55.00` |
| `H13` | `grid_kwh` | `15.0` | `35.0` | `+20.00` |
| `H13` | `battery_action` | `idle` | `charge` | `-` |
| `H13` | `battery_kwh` | `0.0` | `20.0` | `+20.00` |
| `H13` | `battery_energy_after_kwh` | `175.0` | `195.0` | `+20.00` |
| `H14` | `battery_energy_after_kwh` | `240.0` | `260.0` | `+20.00` |
| `H15` | `grid_kwh` | `105.0` | `85.0` | `-20.00` |
| `H15` | `battery_action` | `charge` | `idle` | `-` |
| `H15` | `battery_kwh` | `20.0` | `0.0` | `-20.00` |

---

## 4. Key Findings & Recommendations

1. **Cost Optimality:** The solver achieves the exact optimal cost (0.00 BDT difference) across cases, confirming that the linear programming formulation matches the problem specifications.
2. **Directive Compliance:** Natural-language operator notes (cleaning windows, maintenance lockouts, emergency battery reserves, and transformer limits) are correctly translated and enforced.
3. **Schedule Variance & Peak Demand:** In scenarios like `SAMPLE-09`, multiple charging schedules yield identical daily cost under uniform off-peak tariffs. Adding a slight secondary objective weighting for peak shaving will make the solver prioritize flatter grid profiles when tariffs are tied.
