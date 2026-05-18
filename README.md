#  Remembrall | 
### *Machine Learning Powered Prediction & Event tracking/Analysis*

[![Status](https://img.shields.io/badge/Version-3.0_Stable-blueviolet.svg)](#)
[![ML-Engine](https://img.shields.io/badge/ML-Gradient_Boosting-green.svg)](#)

This tentative beta version moves away from the outdated logic of mainV2.py, sticking back to the traditional slash commands with multiple QOL(autocomplete, check-in button, double verification) features added on with a completed **Gradient Boosting** engine inspired by CatBoost to predict member arrival times with high precision. The name is inspired by the Harry Potter series, where Neville Longbottom is presented with a Remembrall to remind him of what he has forgotten.

---

##  Machine Learning & Logic
Unlike basic trackers, this bot utilizes a **Gradient Boosting Decision Tree (GBDT)** approach to analyze behavioral patterns.

*   **CatBoost Inspiration:** Uses multiple symmetric decision trees. Each tree learns from the residuals of the previous one, weighted by a **Confidence Factor**.
*   **Predict Command:** (In-Testing) Forecasts the likelihood of a member being late based on historical data points.
*   **Schedule Injection:** To keep the ML model efficient, recurring schedules are "injected" as active events **2 hours** prior to their start time, allowing the model to treat all data consistently and giving ample time for earliness.

---

##  Administrative & Multi-Targeting
Built for scale. No more one-by-one commands.

*   **Bulk Operations:** All `create`, `delete`, and `stop` commands support **multiple members and roles** simultaneously for all sorts of events.
*   **UX/DX:** Full **Auto-complete** integration for all command parameters.
*   **Extra Verification:** Deleting or clearing data now triggers a secondary confirmation to prevent accidental nuking of the dataset.

---

##  Event Lifecycle & Automation

### Smart VC Monitoring
*   **The 2h/6h Rule:** 
    *   VC state tracking activates **2 hours** before an event to prevent premature triggers.
    *   A **6-hour late timer** window is enforced; tracking automatically cuts off after this duration to maintain data integrity.

### Interaction Flow
1.  **Creation:** Dynamic buttons for manual check-in are generated instantly.
2.  **Start Time:** Automated DM with check-in buttons sent to all participants at the exact start time.
3.  **The "Nudge":** A **30-minute Re-DM** is automatically triggered for any member who hasn't clicked the check-in button or joined the VC.
4.  **Button Validation** The check-in button would be greyed out once a check in has been confirmed, preventing multiple check-ins of the same event
5.  **Persistence:** Features "Auto Resend DM" buttons and a dedicated `/set-channel` command for log management.

---

##  Command Overview

<details>
<summary><strong> Event Commands</strong></summary>

| Command | Description |
| :--- | :--- |
| `/event create` | Schedule a custom event with a specific date/time for multiple members or roles. |
| `/event create_quick` | Instantly create an event starting in *X minutes* for rapid tracking. |
| `/event stop` | Stops an active event and logs lateness/earliness. Supports bulk targeting. |
| `/event list` | View event history and current active events with filtering added. |
| `/event delete` | Delete a specific event record (with confirmation prompt). |
| `/event clear_all` | Wipe all your event history in the server (multi-step verification). |

</details>

---

<details>
<summary><strong> Recurring Schedule Commands</strong></summary>

| Command | Description |
| :--- | :--- |
| `/event add_schedule` | Create a weekly recurring event with start and end time. |
| `/event list_schedule` | View all your active recurring schedules. |
| `/event delete_schedule` | Remove a recurring schedule (with confirmation). |

</details>

---

<details>
<summary><strong> Prediction command</strong></summary>

| Command | Description |
| :--- | :--- |
| `/event predict` | Uses the Gradient Boosting model inspired from Catboost to forecast lateness for an unstarted event with a confidence range. |

</details>

---

<details>
<summary><strong> Administrative Commands (`/admin`)</strong></summary>

| Command | Description |
| :--- | :--- |
| `/admin set_channel` | Set the log/announcement channel for the bot. |
| `/admin delete` | Delete event records for multiple members or roles. |
| `/admin clear` | Completely wipe event + schedule history for selected users/roles. |
| `/admin stop` | Force-stop events for any members. |
| `/admin add_record` | Manually insert a completed event with lateness data. |
| `/admin add_user_schedule` | Assign recurring schedules to members/roles. |
| `/admin delete_user_schedule` | Remove schedules for members/roles or by ID. |
| `/admin backup` | backs up data to a backup folder and if not creates one |

</details>

---

<details>
<summary><strong> Quality of Life Features</strong></summary>

-  **Full Autocomplete Support** for events & schedules  
-  **Bulk Targeting** (members + roles in one command)  
-  **Multi-Step Confirmation** for destructive actions  
-  **Persistent Check-In System** (auto-resends after restart)  
-  **Automated Nudges** (start + 30-minute reminders)  
-  **ML-Powered Predictions** with confidence intervals  

</details>


##  Development Roadmap
*   **[BETA]** Testing the `/predict` command stability across high-volume servers.
*   **[RESEARCH]** Improving QOL features suggested by the community (scrapping the text recognition for now)

---

##  Installation & Setup

1. **Clone the Repo:**
   ```bash
   git clone [https://github.com/username/lateness-bot-v3.git](https://github.com/username/lateness-bot-v3.git)

## Updates
<details>
<summary><strong> Current Version Updates</strong></summary>

    - **decluttering for cleaner look** time and members are decluttered
    - **added filtering ** for listing, clearing
    - **custom naggin time and notes** notes are not available for create_quick
    - **auto deletion of dms while leaving record** for both vc and check in button
    - **updated some admin functions to be more QOL**
    - **persistent buttons** useable even after bot restarts

