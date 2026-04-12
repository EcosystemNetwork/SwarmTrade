# Design System Document: Kinetic Intelligence Terminal

## 1. Overview & Creative North Star
**Creative North Star: The Sovereign Architect**
This design system moves away from the "friendly" web and leans into the uncompromising aesthetic of high-frequency trading and tactical HUDs. It is a "Sovereign Architect" system—it does not hold the user's hand; it provides a high-fidelity cockpit for autonomous DeFi execution. 

To break the "template" look, we employ **Intentional Asymmetry**. Dashboards should not be perfectly balanced; they should feel like a modular machine where different data modules dock into a void-like framework. By utilizing scan-line overlays and high-contrast typography scales, we create a sense of "Kinetic Intelligence"—a UI that feels alive, processing millions of data points in real-time.

## 2. Colors & Surface Logic
The palette is rooted in the `surface_container_lowest` (#0a0e17), designed to absorb light and push the neon data points to the absolute foreground.

### Surface Hierarchy & Nesting
Depth is not achieved through shadows, but through **Tonal Layering**. 
- **Base Layer:** `surface` (#0f131c) – The foundation of the terminal.
- **Modules:** `surface_container_low` (#181b25) – Primary containers for data streams.
- **Active Focus:** `surface_container_high` (#262a34) – Used for active terminal inputs or hovering states.

### The "No-Line" Rule & Signature Textures
Standard 1px solid borders for sectioning are strictly prohibited. 
- **The Glow Border:** Instead of flat lines, use a 1px inner-glow using the `primary_container` (#00ff88) or `secondary_container` (#00d2fd) at 30% opacity. This mimics a CRT phosphor bleed.
- **Scan-line Texture:** Apply a global fixed-position SVG pattern of 1px horizontal lines at 2% opacity over the entire UI to ground the "Terminal" vibe.
- **Signature Gradients:** For CTAs and critical status indicators, use a linear gradient from `primary` (#f1ffef) to `primary_container` (#00ff88) at a 45-degree angle to create "visual soul."

## 3. Typography
The system utilizes a dual-font strategy to separate **human-readable labels** from **machine-executable data**.

*   **Data & Values (JetBrains Mono / Fira Code):** Every price, percentage, and hash must be rendered in monospaced type. This ensures that fluctuating numbers do not cause horizontal layout shifts (jitter) and reinforces the terminal aesthetic.
*   **System Labels (Inter):** Used for metadata, navigation, and UI instructions. This provides a clean, neutral counterpoint to the aggressive data streams.

### Typography Scale
- **Display-LG (3.5rem, Space Grotesk):** For monumental data points (e.g., Total Portfolio Value).
- **Headline-SM (1.5rem, Space Grotesk):** For section headers within the terminal.
- **Label-MD (0.75rem, Inter):** For all field descriptors. Always uppercase with 0.05em letter spacing.
- **Body-MD (0.875rem, JetBrains Mono):** The workhorse for transaction logs and ledger data.

## 4. Elevation & Depth
In a cyberpunk terminal, depth is "Electronic," not "Organic." 

### The Layering Principle
Avoid traditional structural lines. Place a `surface_container_highest` (#31353f) header against a `surface_container_low` (#181b25) body to create a natural, hard-edged break. 

### Glassmorphism & Depth
For floating modals or "Command Palettes," use `surface_container` with a `backdrop-filter: blur(12px)`. This allows the neon pulses of the background data to bleed through the window, maintaining the user's "situational awareness" of the market while they execute a trade.

### The "Ghost Border" Fallback
Where separation is critical for accessibility, use a **Ghost Border**: `outline_variant` (#3b4b3d) at 20% opacity. It should feel like a faint grid line on a blueprint, not a container edge.

## 5. Components

### Buttons (Tactile Triggers)
- **Primary:** Background `primary_container` (#00ff88), Text `on_primary` (#003919). 0px border-radius. On hover: Add a `box-shadow: 0 0 15px #00ff88`.
- **Secondary:** Transparent background, 1px Ghost Border of `secondary` (#a2e7ff). Text `secondary`. 
- **States:** All interactions must be instant (0ms or 50ms easing) to feel like hardware.

### Input Fields (Terminal Prompts)
- **Style:** Underline only (2px `outline` #849585) or fully enclosed in `surface_container_low`. 
- **Focus:** The underline shifts to `secondary_fixed` (#b4ebff) with a subtle outer glow. The cursor should be a solid block, blinking at a 1s interval.

### Cards & Lists (Data Cells)
- **The Rule:** No dividers. Use 8px or 16px of vertical white space to separate list items. 
- **Visual Signal:** Use a 2px vertical "intent bar" on the far left of a list item to indicate status (e.g., `primary_container` for a filled order, `error` for a cancelled one).

### Additional Component: "The Ticker Tape"
A continuous, high-density scrolling horizontal bar at the top or bottom of the screen using `label-sm` to display global market sentiment and gas prices.

## 6. Do's and Don'ts

### Do:
- **Embrace Density:** Information density is a feature, not a bug. Use every pixel for data.
- **Keep it Sharp:** 0px border radius across all elements. No exceptions.
- **Use Kinetic Color:** Use `primary_fixed_dim` (#00e479) for positive movement and `tertiary_fixed_dim` (#ffb2ba) for negative movement.

### Don't:
- **Don't use Rounded Corners:** This dilutes the "Terminal" authority and makes it feel like a consumer app.
- **Don't use Soft Shadows:** Traditional drop shadows have no place in a digital HUD. Use glows or tonal shifts.
- **Don't use Center-Alignment:** Most data should be left-aligned for rapid scanning or right-aligned for numerical comparison. Center alignment is too "editorial" and slows down data processing.