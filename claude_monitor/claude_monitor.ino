/*
 * Claude Usage Monitor — ESP8266 (Wemos D1 Mini) + ST7735 1.8" TFT
 * ------------------------------------------------------------------
 * Polls bridge.py and shows SESSION (5h block) and WEEKLY usage as two
 * "instrument cards": a big color-coded % hero, a size-2 reset countdown,
 * and a segmented meter. Design from the design-package.
 *
 * Libraries (Arduino IDE > Library Manager):
 *   - Adafruit GFX Library
 *   - Adafruit ST7735 and ST7789 Library
 *   - ArduinoJson  (v7.x, by Benoit Blanchon)
 *
 * Board: "LOLIN(WEMOS) D1 R2 & mini"
 *
 * Wiring (TFT -> D1 Mini):
 *   VCC -> 3V3      GND -> GND       LED -> 3V3
 *   CS  -> D8 (GPIO15)               A0/DC -> D3 (GPIO0)
 *   RESET -> D4 (GPIO2)             SDA/MOSI -> D7 (GPIO13)   SCK -> D5 (GPIO14)
 */

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiClientSecureBearSSL.h>   // HTTPS to api.anthropic.com (direct-API mode)
#include <LittleFS.h>                  // on-device token store (DEVICE_MANAGES_TOKENS)
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include <SPI.h>
#include <math.h>
#include <time.h>                      // NTP-derived clock for reset countdowns

// ====================== USER CONFIG ======================
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

// --- Data source -----------------------------------------------------------
// 1 = DIRECT: device fetches a fresh bearer token from token_bridge.py (/token)
//     over the LAN, then calls api.anthropic.com/api/oauth/usage ITSELF over
//     HTTPS and parses the plan 5h/7d utilization. The bridge only vends the
//     token (the ESP8266 can't do the OAuth refresh dance); the usage data
//     comes straight from Anthropic. This needs NTP (to turn the API's absolute
//     resets_at into a countdown) and ~16 KB of heap for the TLS buffers.
// 0 = PROXY: device polls token_bridge.py's /usage and the bridge does the API
//     call + time math. Lighter on the device; rock-solid fallback.
#define USE_DIRECT_API 1

// DEVICE_MANAGES_TOKENS (requires USE_DIRECT_API 1):
// 1 = STANDALONE. The device pulls the full credential bundle from the bridge's
//     /provision ONCE (into LittleFS), then refreshes its OWN OAuth tokens
//     (rotating refresh token persisted to flash) and never needs the bridge
//     again. Run the bridge with PROVISION_ONLY=1 so it doesn't also refresh
//     (two refreshers would invalidate each other's single-use refresh token).
// 0 = the device fetches a fresh bearer from the bridge's /token each poll
//     (the bridge owns refresh; the Mac must stay running).
#define DEVICE_MANAGES_TOKENS 1

// PROXY mode: the /usage URL token_bridge.py prints when it starts.
const char* SERVER_URL = "http://192.168.1.4:8088/usage";

// DIRECT mode endpoints. TOKEN_URL (bridge /token) is used only when
// DEVICE_MANAGES_TOKENS = 0; PROVISION_URL (bridge /provision) only when = 1.
const char* TOKEN_URL      = "http://192.168.1.4:8088/token";
const char* PROVISION_URL  = "http://192.168.1.4:8088/provision";
const char* USAGE_API_URL  = "https://api.anthropic.com/api/oauth/usage";
const char* USAGE_API_HOST = "api.anthropic.com";           // for MFLN probe
const char* ANTHROPIC_BETA = "oauth-2025-04-20";            // required header value

// Token refresh (DEVICE_MANAGES_TOKENS): values lifted from the Claude Code binary.
const char* TOKEN_REFRESH_URL  = "https://platform.claude.com/v1/oauth/token";
const char* TOKEN_REFRESH_HOST = "platform.claude.com";     // for MFLN probe
const char* OAUTH_CLIENT_ID    = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";

// AUTO_START_SESSION (requires DEVICE_MANAGES_TOKENS 1): when an account has no
// active 5h block (its five_hour.resets_at is null), send ONE minimal message to
// the cheapest model to anchor the block. Costs ~22 input + 1 output Haiku
// tokens (rounds to 0% usage). Fires once per idle period (per-account latch).
// Set to 0 to disable. Verified live: resets_at goes null -> a timestamp.
#define AUTO_START_SESSION 1
#if AUTO_START_SESSION
const char* ANTHROPIC_VERSION  = "2023-06-01";
const char* MESSAGES_API_URL   = "https://api.anthropic.com/v1/messages";
const char* CHEAP_MODEL        = "claude-haiku-4-5";
const char* CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude.";
#endif

// api.anthropic.com/api/oauth/usage is rate-limited (returns 429 if hit too
// often). Usage %s move slowly, so poll gently: 5 min keeps us well clear with
// 2 accounts (~24 requests/hour). Going much lower risks 429 -> a frozen screen.
const unsigned long POLL_MS = 300000;    // 5 minutes
const unsigned long ACCOUNT_DWELL_MS = 5000;  // with >1 account, how long each is shown

// TFT control pins
#define TFT_CS   15   // D8
#define TFT_DC    0   // D3 (A0)
#define TFT_RST   2   // D4
// MOSI = D7 (GPIO13), SCK = D5 (GPIO14) are fixed hardware-SPI pins.

// Panel variant: change to INITR_GREENTAB / INITR_REDTAB if colors/offset look wrong.
#define TFT_INITTAB INITR_BLACKTAB
// =========================================================

Adafruit_ST7735 tft = Adafruit_ST7735(TFT_CS, TFT_DC, TFT_RST);

// 16-bit (565) palette
#define COL_BG      0x0000
#define COL_TEXT    0xFFFF
#define COL_DIM     0x8410
#define COL_TRACK   0x2104
#define COL_GREEN   0x2DEA
#define COL_YELLOW  0xFE60
#define COL_RED     0xF9A6
#define COL_ACCENT  0x055F   // structural accent (cyan-blue)
#define COL_CLAUDE  0xDBAA   // Claude warm orange

const int W = 160, H = 128;

// ---- state ----
unsigned long lastPoll  = 0;
bool          firstDraw = true;     // true until the dashboard is first painted
bool          lastOk    = false;

// ---- animation ----
const unsigned long FRAME_MS = 33;  // ~30 fps
unsigned long lastFrame   = 0;
float         animPhase   = 0;      // time-based (millis) -> smooth
unsigned long rippleStart = 0;      // millis() of last data arrival (spark "pop")
String        bootSub     = "Starting...";
String        accountLabel = "";
String        lastBootSub  = "";

// ---- cached last-good values (so the stale state can re-render dimmed) ----
String cAccount = "";
int   cSPct = 0, cSReset = 0;  bool cSActive = false;
int   cWPct = 0, cWReset = 0, cWDays = 0;
bool  haveData = false, sessRed = false, weekRed = false;

// ---- multi-account rotation (bridge serves an accounts[] array) ----
#define MAX_ACCOUNTS 8
struct AcctView {
  String label; bool active;
  int   sPct, sReset; bool sActive;
  int   wPct, wReset, wDays;
};
AcctView      accts[MAX_ACCOUNTS];
int           acctCount  = 0;        // how many accounts the bridge reported
int           acctIdx    = 0;        // which one is on screen right now
unsigned long lastSwitch = 0;        // millis() of last rotation

// off-screen sprite for the boot spark (flicker-free single blit)
#define SPR 52
GFXcanvas16 sparkSprite(SPR, SPR);

// header status spark: rendered at 3x then box-downsampled for anti-aliased,
// smooth (jitter-free) rotation. Output is 16x12 and sits ABOVE the rule.
#define HSPR_W  16
#define HSPR_H  12
#define HSPR_SS 3                                       // supersample factor
GFXcanvas16 hdrSprite(HSPR_W, HSPR_H);                  // downsampled output (blitted)
GFXcanvas16 hdrHi(HSPR_W * HSPR_SS, HSPR_H * HSPR_SS);  // 48x36 hi-res render

// ===========================================================================
// small helpers
// ===========================================================================
uint16_t barColor(int pct) {
  if (pct >= 85) return COL_RED;
  if (pct >= 60) return COL_YELLOW;
  return COL_GREEN;
}


void txt(int x, int y, const String& s, uint16_t col, uint8_t size) {
  tft.setTextColor(col); tft.setTextSize(size); tft.setCursor(x, y); tft.print(s);
}
int tw(const String& s, uint8_t size) { return s.length() * 6 * size; }

// compact duration: "47m" / "4h24m" / "4d 15h"
String fmtDur(int minutes) {
  if (minutes <= 0) return "--";
  char buf[16];
  int d = minutes / 1440;
  if (d >= 1) { int h = (minutes % 1440) / 60; snprintf(buf, sizeof(buf), "%dd %dh", d, h); }
  else {
    int h = minutes / 60, m = minutes % 60;
    if (h > 0) snprintf(buf, sizeof(buf), "%dh%02dm", h, m);
    else       snprintf(buf, sizeof(buf), "%dm", m);
  }
  return String(buf);
}

// tiny 7px clock glyph (circle + two hands)
void drawClock(int cx, int cy, uint16_t col) {
  tft.drawCircle(cx, cy, 3, col);
  tft.drawLine(cx, cy, cx, cy - 2, col);
  tft.drawLine(cx, cy, cx + 2, cy, col);
}

// small outlined pill badge; returns its width
int drawBadge(int x, int y, const String& s, uint16_t col) {
  int w = s.length() * 6 + 5;
  tft.drawRoundRect(x, y, w, 10, 2, col);
  txt(x + 3, y + 2, s, col, 1);
  return w;
}

// segmented level meter: solid fill sliced by 1px BG notches + border
void drawMeter(int x, int y, int w, int h, int pct, bool idle, uint16_t col) {
  pct = constrain(pct, 0, 100);
  tft.fillRect(x, y, w, h, COL_TRACK);
  if (!idle) {
    int fw = (w * pct + 50) / 100;
    if (fw > 0) tft.fillRect(x, y, fw, h, col);
  }
  for (int i = 1; i < 10; i++) tft.drawFastVLine(x + (w * i + 5) / 10, y, h, COL_BG);
  tft.drawRect(x, y, w, h, (col == COL_RED) ? COL_RED : COL_DIM);
}

// ===========================================================================
// Claude spark — header status light (smooth rotation via supersampling)
// ===========================================================================
// Render the spinning 8-point spark at HSPR_SS x scale, then average each
// HSPR_SS x HSPR_SS block down -> anti-aliased edges that fade between pixels
// as it turns, so the rotation looks smooth instead of snapping. One blit, no
// flicker, confined to y 0..11 so it never touches the header rule at y=12.
void drawHeaderSpark(float rot, uint16_t color, bool pop) {
  hdrHi.fillScreen(COL_BG);
  const float cx = HSPR_W * HSPR_SS / 2.0f, cy = HSPR_H * HSPR_SS / 2.0f;
  const int   outer = 5 * HSPR_SS, inner = 2 * HSPR_SS, rays = 8;
  const float da = 0.42f;
  for (int i = 0; i < rays; i++) {
    float a = rot + i * (6.2831853f / rays);
    int tx  = (int)lroundf(cx + cosf(a)      * outer), ty  = (int)lroundf(cy + sinf(a)      * outer);
    int b1x = (int)lroundf(cx + cosf(a + da) * inner), b1y = (int)lroundf(cy + sinf(a + da) * inner);
    int b2x = (int)lroundf(cx + cosf(a - da) * inner), b2y = (int)lroundf(cy + sinf(a - da) * inner);
    hdrHi.fillTriangle(tx, ty, b1x, b1y, b2x, b2y, color);
  }
  hdrHi.fillCircle((int)cx, (int)cy, inner, color);
  hdrHi.fillCircle((int)cx, (int)cy, HSPR_SS, pop ? COL_TEXT : color);   // bright core on fresh data

  // box-downsample HSPR_SS x HSPR_SS blocks -> anti-aliased output
  uint16_t* hi  = hdrHi.getBuffer();
  uint16_t* out = hdrSprite.getBuffer();
  const int HW = HSPR_W * HSPR_SS, n = HSPR_SS * HSPR_SS;
  for (int oy = 0; oy < HSPR_H; oy++) {
    for (int ox = 0; ox < HSPR_W; ox++) {
      int rs = 0, gs = 0, bs = 0;
      for (int dy = 0; dy < HSPR_SS; dy++) {
        const uint16_t* row = hi + (oy * HSPR_SS + dy) * HW + ox * HSPR_SS;
        for (int dx = 0; dx < HSPR_SS; dx++) {
          uint16_t p = row[dx];
          rs += (p >> 11) & 0x1F; gs += (p >> 5) & 0x3F; bs += p & 0x1F;
        }
      }
      out[oy * HSPR_W + ox] = ((rs / n) << 11) | ((gs / n) << 5) | (bs / n);
    }
  }
}

// top-right status light: orange when ok / red when stale, rotating smoothly,
// with a bright flash for ~450 ms when fresh data arrives.
void headerSpark(bool ok) {
  bool pop = (millis() - rippleStart) < 450;
  uint16_t c = ok ? COL_CLAUDE : COL_RED;
  drawHeaderSpark(animPhase * 0.25f, c, pop);            // slow, smooth rotation
  tft.drawRGBBitmap(144, 0, hdrSprite.getBuffer(), HSPR_W, HSPR_H);  // y0..11, above the rule
}

// blinking "maxed out" warning triangle next to a card label
void blinkWarn(int wx, int y0, bool show) {
  if (show) {
    tft.fillTriangle(wx - 5, y0 + 8, wx + 5, y0 + 8, wx, y0 - 1, COL_RED);
    tft.fillRect(wx, y0 + 2, 1, 3, COL_BG);
    tft.fillRect(wx, y0 + 6, 1, 1, COL_BG);
  } else {
    tft.fillRect(wx - 5, y0 - 1, 11, 10, COL_BG);
  }
}
void dashWarnings() {
  bool on = (millis() % 900) < 450;
  if (sessRed) blinkWarn(80, 16, on);
  if (weekRed) blinkWarn(74, 75, on);
}

// ===========================================================================
// boot splash
// ===========================================================================
void putTextCentered(int y, const String& s, uint16_t color, uint8_t size) {
  int w = s.length() * 6 * size;
  int x = (W - w) / 2; if (x < 0) x = 0;
  tft.fillRect(0, y, W, 8 * size, COL_BG);
  txt(x, y, s, color, size);
}

void drawSparkSprite(float t, uint16_t color) {
  sparkSprite.fillScreen(COL_BG);
  const int cx = SPR / 2, cy = SPR / 2;
  float rot = t * 0.5f, breathe = 0.72f + 0.28f * sinf(t * 1.6f);
  int outer = (int)(18 * breathe);
  const float da = 0.45f;
  for (int i = 0; i < 8; i++) {
    float a = rot + i * (6.2831853f / 8);
    int tx  = cx + (int)(cosf(a)      * outer), ty  = cy + (int)(sinf(a)      * outer);
    int b1x = cx + (int)(cosf(a + da) * 5), b1y = cy + (int)(sinf(a + da) * 5);
    int b2x = cx + (int)(cosf(a - da) * 5), b2y = cy + (int)(sinf(a - da) * 5);
    sparkSprite.fillTriangle(tx, ty, b1x, b1y, b2x, b2y, color);
  }
  sparkSprite.fillCircle(cx, cy, 5, color);
  sparkSprite.fillCircle(cx, cy, 3, COL_TEXT);
  tft.drawRGBBitmap(80 - SPR / 2, 36 - SPR / 2, sparkSprite.getBuffer(), SPR, SPR);
}

void splashBegin() {
  tft.fillScreen(COL_BG);
  putTextCentered(64, "CLAUDE", COL_TEXT, 2);
  putTextCentered(84, "usage monitor", COL_DIM, 1);
  lastBootSub = "";
}

void splashFrame(const String& sub) {
  bool err = (sub.indexOf("Wait") >= 0 || sub.indexOf("fail") >= 0 || sub.indexOf("error") >= 0);
  drawSparkSprite(animPhase, err ? COL_RED : COL_CLAUDE);

  int tx = 34, barW = 92, ty = 104;             // progress track
  tft.fillRect(tx, ty, barW, 3, COL_TRACK);
  if (err) {
    if ((millis() % 900) < 450) tft.fillRect(tx, ty, barW, 3, COL_RED);   // pulse = stuck
  } else {
    int seg = 22, span = barW - seg;
    float p = sinf(animPhase * 1.3f) * 0.5f + 0.5f;                       // ping-pong sweep
    tft.fillRect(tx + (int)(span * p), ty, seg, 3, COL_ACCENT);
  }
  if (sub != lastBootSub) { putTextCentered(114, sub, err ? COL_RED : COL_DIM, 1); lastBootSub = sub; }
}

// ===========================================================================
// dashboard
// ===========================================================================
void drawHeader(bool ok) {
  tft.fillRect(0, 0, 144, 11, COL_BG);          // account band (leaves the spark alone)
  String acct = cAccount.length() ? cAccount : String("CLAUDE USAGE");
  int maxChars = 23;
  if (acctCount > 1) {                          // "n/N" page indicator, right-aligned
    String idxs = String(acctIdx + 1) + "/" + String(acctCount);
    int iw = tw(idxs, 1);
    bool active = accts[acctIdx].active;         // highlight when viewing the live account
    txt(143 - iw, 2, idxs, ok ? (active ? COL_CLAUDE : COL_DIM) : COL_RED, 1);
    maxChars = (143 - iw - 9) / 6;               // squeeze the label in beside it
    if (maxChars < 4) maxChars = 4;
  }
  if ((int)acct.length() > maxChars) acct = acct.substring(0, maxChars);
  txt(5, 2, acct, ok ? COL_DIM : COL_RED, 1);
  tft.drawFastHLine(0, 12, 160, ok ? COL_ACCENT : COL_RED);
}

// One metric as an instrument card at y0. idle => calm "IDLE" + empty meter.
void drawCard(int y0, const String& label, const String& badge, int pct, bool idle,
              int resetMin, bool ok) {
  uint16_t col    = idle ? COL_DIM : barColor(pct);
  bool     red    = (!idle && ok && pct >= 85);
  uint16_t numCol = ok ? col : COL_DIM;          // freeze value to DIM when stale

  // label + period badge
  tft.fillRect(6, y0 - 1, 88, 11, COL_BG);
  txt(6, y0, label, ok ? COL_CLAUDE : COL_DIM, 1);
  drawBadge(6 + tw(label, 1) + 5, y0 - 1, badge, COL_DIM);

  // BIG % hero (left)
  tft.fillRect(6, y0 + 11, 88, 27, COL_BG);
  if (idle) {
    txt(6, y0 + 15, "IDLE", COL_DIM, 2);
  } else {
    String ps = String(pct);
    txt(6, y0 + 11, ps, numCol, 3);
    txt(6 + tw(ps, 3) + 2, y0 + 19, "%", numCol, 2);
  }

  // reset readout (right): RESETS label + clock + size-2 value
  txt(154 - tw("RESETS", 1), y0 + 11, "RESETS", COL_DIM, 1);
  String rs = idle ? String("idle") : fmtDur(resetMin);
  tft.fillRect(94, y0 + 22, 60, 16, COL_BG);
  int rw = tw(rs, 2);
  drawClock(154 - rw - 8, y0 + 27, idle ? COL_DIM : COL_CLAUDE);
  txt(154 - rw, y0 + 22, rs, idle ? COL_DIM : COL_TEXT, 2);

  // segmented meter
  drawMeter(6, y0 + 40, 148, 7, idle ? 0 : pct, idle, idle ? COL_DIM : (red ? COL_RED : col));
}

void drawAll(bool ok) {
  if (firstDraw) { tft.fillScreen(COL_BG); firstDraw = false; }   // clear the splash
  drawHeader(ok);

  sessRed = ok && cSActive && cSPct >= 85;
  drawCard(16, "SESSION", "5h", cSActive ? cSPct : 0, !cSActive,
           cSActive ? cSReset : 0, ok);

  tft.drawFastHLine(6, 71, 148, COL_TRACK);                       // divider

  weekRed = ok && cWPct >= 85;
  drawCard(75, "WEEKLY", String(cWDays) + "d", cWPct, false,
           cWReset, ok);
}

// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(100);

  tft.initR(TFT_INITTAB);
  tft.setRotation(3);
  tft.setSPISpeed(40000000);   // 40 MHz SPI (lower to 24000000 if the panel glitches)
  splashBegin();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);   // non-blocking; loop() animates the splash meanwhile
  bootSub = "Connecting WiFi...";
#if USE_DIRECT_API
  // UTC clock (offset 0, no DST) so resets_at -> countdown math is correct.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
#if DEVICE_MANAGES_TOKENS
  if (!LittleFS.begin()) {                 // mount the token store; format if first boot
    Serial.println("LittleFS mount failed, formatting...");
    LittleFS.format(); LittleFS.begin();
  }
#endif
#endif
  lastPoll = millis() - POLL_MS;
}

#if USE_DIRECT_API
// --- DIRECT mode: device calls api.anthropic.com/api/oauth/usage itself -----

// days since 1970-01-01 for a y-m-d date (Howard Hinnant's civil algorithm).
static long daysFromCivil(int y, int m, int d) {
  y -= (m <= 2);
  long era = (y >= 0 ? y : y - 399) / 400;
  long yoe = y - era * 400;
  long doy = (153L * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  long doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097L + doe - 719468L;
}

// resets_at -> epoch seconds (UTC). Accepts an ISO-8601 string as the API sends
// it ("2026-06-18T05:00:00.258226+00:00" -- fractional seconds ignored, trailing
// timezone offset honored; bare/"Z" = UTC), or a numeric epoch in s or ms.
static long resetsEpoch(JsonVariantConst v) {
  if (v.is<const char*>()) {
    const char* str = v.as<const char*>();
    int Y = 0, Mo = 0, D = 0, h = 0, mi = 0, se = 0;
    if (sscanf(str, "%d-%d-%dT%d:%d:%d", &Y, &Mo, &D, &h, &mi, &se) < 3) return 0;
    long ep = daysFromCivil(Y, Mo, D) * 86400L + h * 3600L + mi * 60L + se;
    // Honor a trailing offset (e.g. "+00:00", "-07:00"): convert the wall-clock
    // time to UTC. '+'/'-' only ever appear AFTER the 'T' in the offset, so this
    // can't trip on the date's dashes. No offset or 'Z' => already UTC.
    const char* t = strchr(str, 'T');
    const char* tz = t ? strpbrk(t, "+-") : nullptr;
    if (tz) {
      int oh = 0, om = 0;
      if (sscanf(tz + 1, "%d:%d", &oh, &om) >= 1)
        ep -= (*tz == '-' ? -1L : 1L) * (oh * 3600L + om * 60L);
    }
    return ep;
  }
  double n = v.as<double>();
  if (n <= 0) return 0;
  return (long)(n > 1e11 ? n / 1000.0 : n);     // ms vs s
}

// minutes from now until resets_at (needs NTP; 0 if unknown / clock not synced).
static int resetsInMin(JsonVariantConst v) {
  long ep = resetsEpoch(v);
  time_t now = time(nullptr);
  if (ep <= 0 || now < 1000000000L) return 0;   // now<2001 => NTP not ready yet
  long m = (ep - (long)now) / 60;
  return m > 0 ? (int)m : 0;
}

// Query usage for ONE account and append it to the accounts[] array. Returns the
// HTTP status (200 = parsed + appended), or -1 (couldn't connect) / -2 (bad JSON).
// Callers refresh + retry when this is 401/403.
static int fetchOneAccount(JsonArray accts, const char* bearer, const char* label) {
  BearSSL::WiFiClientSecure tls;
  tls.setInsecure();                              // personal gadget: skip cert pinning
  // Probe MFLN ONCE and cache it (each probe is a separate TLS connection -- no
  // need to repeat it every poll). MFLN lets us use a small RX buffer; if the
  // server doesn't support it we keep the full 16 KB RX but still shrink TX.
  static int mfln = -1;
  if (mfln < 0) mfln = tls.probeMaxFragmentLength(USAGE_API_HOST, 443, 1024) ? 1 : 0;
  tls.setBufferSizes(mfln ? 1024 : 16384, 512);

  HTTPClient https;
  https.setTimeout(10000);
  if (!https.begin(tls, USAGE_API_URL)) return -1;
  https.addHeader("Authorization", String("Bearer ") + bearer);
  https.addHeader("anthropic-beta", ANTHROPIC_BETA);
  int code = https.GET();
  if (code == 200) {
    // getString() (not getStream()) so HTTPClient de-chunks the response --
    // the API uses Transfer-Encoding: chunked, whose framing isn't valid JSON.
    String body = https.getString();
    JsonDocument raw;
    DeserializationError err = deserializeJson(raw, body);
    if (!err) {
      JsonObjectConst fh = raw["five_hour"];
      JsonObjectConst sd = raw["seven_day"];
      JsonObject a = accts.add<JsonObject>();
      a["account"] = String(label[0] ? label : "Claude");   // String() => copied into doc
      a["active"]  = (accts.size() == 1);                    // first drawn gets the highlight
      JsonObject s = a["session"].to<JsonObject>();
      s["pct"]           = (int)lround((double)(fh["utilization"] | 0.0));
      s["active"]        = !fh.isNull();
      s["has_block"]     = !fh["resets_at"].isNull();   // false == idle (no active 5h block)
      s["resets_in_min"] = resetsInMin(fh["resets_at"]);
      JsonObject w = a["weekly"].to<JsonObject>();
      w["pct"]           = (int)lround((double)(sd["utilization"] | 0.0));
      w["resets_in_min"] = resetsInMin(sd["resets_at"]);
      w["days"]          = 7;
    } else {
      Serial.print("API JSON err: "); Serial.println(err.c_str());
      code = -2;
    }
  } else {
    Serial.print("API HTTP "); Serial.println(code);
  }
  https.end();
  return code;
}

// DIRECT mode: fetch the per-account bearer list from token_bridge.py (/token),
// then call api.anthropic.com/api/oauth/usage ourselves for EACH account and
// normalize into the accounts[] JSON the proxy path produces, so render() (and
// the existing multi-account rotation) is reused unchanged. The HTTPS calls are
// sequential, so peak TLS RAM stays at a single connection no matter how many
// accounts you've minted.
bool fetchUsageDirect(JsonDocument& doc) {
  if (WiFi.status() != WL_CONNECTED) return false;

  JsonDocument tok;                               // holds the token list this poll
  {                                               // 1) one plain-HTTP GET to the bridge
    WiFiClient client;
    HTTPClient http;
    http.setTimeout(6000);
    if (!http.begin(client, TOKEN_URL)) return false;
    int code = http.GET();
    bool tokOk = (code == 200) && !deserializeJson(tok, http.getStream());
    http.end();
    if (!tokOk) { Serial.print("token fetch failed, HTTP "); Serial.println(code); return false; }
  }
  JsonArrayConst toks = tok["accounts"].as<JsonArrayConst>();
  if (toks.isNull() || toks.size() == 0) { Serial.println("no tokens from bridge"); return false; }

  doc.clear();
  doc["ok"] = true;
  JsonArray accts = doc["accounts"].to<JsonArray>();
  bool any = false;
  int n = 0;
  for (JsonObjectConst t : toks) {                // 2) one HTTPS usage call per account
    if (n++ >= MAX_ACCOUNTS) break;
    const char* bearer = t["token"] | "";
    if (!bearer[0]) continue;
    if (fetchOneAccount(accts, bearer, t["label"] | "") == 200) any = true;
  }
  return any;
}

#if DEVICE_MANAGES_TOKENS
// ===========================================================================
// STANDALONE: device stores + refreshes its own OAuth tokens (no bridge after
// the one-time /provision). Refresh tokens are single-use and ROTATE, so we
// persist the rotated bundle to LittleFS the instant a refresh returns.
// ===========================================================================
struct Acct { String label, accessToken, refreshToken; bool needsProvision; bool sessionStarted; };
Acct  tokenStore[MAX_ACCOUNTS];
int   tokenStoreCount = 0;
bool  credsLoaded     = false;
const char* CREDS_PATH = "/creds.json";

// Read the saved bundle from flash into tokenStore[].
bool loadCreds() {
  tokenStoreCount = 0;
  File f = LittleFS.open(CREDS_PATH, "r");
  if (!f) return false;
  JsonDocument d;
  DeserializationError err = deserializeJson(d, f);
  f.close();
  if (err) return false;
  for (JsonObjectConst a : d["accounts"].as<JsonArrayConst>()) {
    if (tokenStoreCount >= MAX_ACCOUNTS) break;
    Acct& t = tokenStore[tokenStoreCount];
    t.label        = String((const char*)(a["label"] | ""));
    t.accessToken  = String((const char*)(a["accessToken"] | ""));
    t.refreshToken = String((const char*)(a["refreshToken"] | ""));
    t.needsProvision = false;
    t.sessionStarted = false;
    if (t.refreshToken.length()) tokenStoreCount++;
  }
  return tokenStoreCount > 0;
}

// Persist tokenStore[] to flash. Write to a temp file then atomically rename
// over the live file, so a power loss can't leave a half-written creds file.
bool saveCreds() {
  JsonDocument d;
  JsonArray arr = d["accounts"].to<JsonArray>();
  for (int i = 0; i < tokenStoreCount; i++) {
    JsonObject a = arr.add<JsonObject>();
    a["label"]        = tokenStore[i].label;
    a["accessToken"]  = tokenStore[i].accessToken;
    a["refreshToken"] = tokenStore[i].refreshToken;
  }
  File f = LittleFS.open("/creds.tmp", "w");
  if (!f) return false;
  serializeJson(d, f);
  f.close();
  if (LittleFS.rename("/creds.tmp", CREDS_PATH)) return true;   // atomic replace
  LittleFS.remove(CREDS_PATH);                                  // fallback for cores that
  return LittleFS.rename("/creds.tmp", CREDS_PATH);             // won't rename over a file
}

// One-time (or lockout-recovery) hand-off: pull the full bundle from the bridge.
bool provision() {
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(8000);
  if (!http.begin(client, PROVISION_URL)) return false;
  int code = http.GET();
  bool ok = false;
  if (code == 200) {
    JsonDocument d;
    if (!deserializeJson(d, http.getStream())) {
      tokenStoreCount = 0;
      for (JsonObjectConst a : d["accounts"].as<JsonArrayConst>()) {
        if (tokenStoreCount >= MAX_ACCOUNTS) break;
        Acct& t = tokenStore[tokenStoreCount];
        t.label        = String((const char*)(a["label"] | ""));
        t.accessToken  = String((const char*)(a["access_token"] | ""));
        t.refreshToken = String((const char*)(a["refresh_token"] | ""));
        t.needsProvision = false;
        t.sessionStarted = false;
        if (t.refreshToken.length()) tokenStoreCount++;
      }
      if (tokenStoreCount > 0) { saveCreds(); ok = true; }
    }
  } else {
    Serial.print("provision HTTP "); Serial.println(code);
  }
  http.end();
  return ok;
}

// Exchange this account's refresh token for a fresh access token, persisting the
// rotated refresh token immediately. Returns true on success.
bool refreshAccount(int i) {
  if (i < 0 || i >= tokenStoreCount || !tokenStore[i].refreshToken.length()) return false;
  JsonDocument body;
  body["grant_type"]   = "refresh_token";
  body["refresh_token"] = tokenStore[i].refreshToken;
  body["client_id"]    = OAUTH_CLIENT_ID;
  String payload;
  serializeJson(body, payload);

  BearSSL::WiFiClientSecure tls;
  tls.setInsecure();
  static int mfln = -1;
  if (mfln < 0) mfln = tls.probeMaxFragmentLength(TOKEN_REFRESH_HOST, 443, 1024) ? 1 : 0;
  tls.setBufferSizes(mfln ? 1024 : 16384, 512);
  HTTPClient https;
  https.setTimeout(12000);
  if (!https.begin(tls, TOKEN_REFRESH_URL)) return false;
  https.addHeader("Content-Type", "application/json");
  int code = https.POST(payload);
  bool ok = false;
  if (code == 200) {
    String body = https.getString();             // de-chunk before parsing
    JsonDocument d;
    if (!deserializeJson(d, body)) {
      const char* at = d["access_token"]  | "";
      const char* rt = d["refresh_token"] | "";
      if (at[0]) {
        tokenStore[i].accessToken = at;            // String op= copies out of d
        if (rt[0]) tokenStore[i].refreshToken = rt;  // rotation: keep the new one
        saveCreds();                                 // persist NOW (minimize loss window)
        ok = true;
      }
    }
  } else {
    Serial.print("refresh HTTP "); Serial.println(code);
    if (code == 400 || code == 401 || code == 403) tokenStore[i].needsProvision = true;
  }
  https.end();
  return ok;
}

#if AUTO_START_SESSION
// Anchor an idle 5h block by sending ONE minimal message AS Claude Code (the
// OAuth token is gated to that identity, so the system prompt is mandatory).
// Cheapest model + max_tokens 1 -> ~22 input / 1 output tokens. Returns true on 200.
bool startSession(const char* bearer) {
  BearSSL::WiFiClientSecure tls;
  tls.setInsecure();
  static int mfln = -1;                                 // same host as the usage call
  if (mfln < 0) mfln = tls.probeMaxFragmentLength(USAGE_API_HOST, 443, 1024) ? 1 : 0;
  tls.setBufferSizes(mfln ? 1024 : 16384, 512);
  HTTPClient https;
  https.setTimeout(12000);
  if (!https.begin(tls, MESSAGES_API_URL)) return false;
  https.addHeader("Authorization", String("Bearer ") + bearer);
  https.addHeader("anthropic-version", ANTHROPIC_VERSION);
  https.addHeader("anthropic-beta", ANTHROPIC_BETA);
  https.addHeader("Content-Type", "application/json");
  JsonDocument body;                                    // build it so strings are escaped
  body["model"]      = CHEAP_MODEL;
  body["max_tokens"] = 1;
  body["system"]     = CLAUDE_CODE_SYSTEM;
  JsonObject m = body["messages"].to<JsonArray>().add<JsonObject>();
  m["role"] = "user"; m["content"] = "hi";
  String payload;
  serializeJson(body, payload);
  int code = https.POST(payload);
  https.end();
  if (code == 200) { Serial.println("auto-start: 5h session started"); return true; }
  Serial.print("auto-start HTTP "); Serial.println(code);
  return false;
}
#endif

// STANDALONE poll: load creds (first time), (re)provision if empty or locked out,
// then query each account, refreshing on a 401 and retrying once.
bool fetchUsageManaged(JsonDocument& doc) {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!credsLoaded) { loadCreds(); credsLoaded = true; }

  bool needProv = (tokenStoreCount == 0);
  for (int i = 0; i < tokenStoreCount; i++)
    if (tokenStore[i].needsProvision) needProv = true;
  if (needProv) provision();                       // first run, or re-mint recovery
  if (tokenStoreCount == 0) return false;

  doc.clear();
  doc["ok"] = true;
  JsonArray accts = doc["accounts"].to<JsonArray>();
  bool any = false;
  for (int i = 0; i < tokenStoreCount; i++) {
    int code = fetchOneAccount(accts, tokenStore[i].accessToken.c_str(), tokenStore[i].label.c_str());
    if ((code == 401 || code == 403) && refreshAccount(i))      // token expired -> refresh + retry
      code = fetchOneAccount(accts, tokenStore[i].accessToken.c_str(), tokenStore[i].label.c_str());
    if (code == 200) {
      any = true;
#if AUTO_START_SESSION
      // No active 5h block (resets_at null) -> anchor one, once per idle period.
      // Gate on a synced clock so NTP warm-up doesn't look like "idle".
      bool hasBlock = (bool)(accts[accts.size() - 1]["session"]["has_block"] | true);
      if (hasBlock) {
        tokenStore[i].sessionStarted = false;            // block exists -> rearm for next idle
      } else {
        if (tokenStore[i].sessionStarted)                // stale latch — allow retry
          tokenStore[i].sessionStarted = false;
        if (time(nullptr) > 1000000000L
            && startSession(tokenStore[i].accessToken.c_str())) {
          fetchOneAccount(accts, tokenStore[i].accessToken.c_str(),
                          tokenStore[i].label.c_str());  // refresh card; retry next poll if still idle
        }
      }
#endif
    }
    else if (code == 401 || code == 403) tokenStore[i].needsProvision = true;  // re-provision next poll
  }
  return any;
}
#endif  // DEVICE_MANAGES_TOKENS
#endif  // USE_DIRECT_API

bool fetchUsage(JsonDocument& doc) {
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(8000);
  if (!http.begin(client, SERVER_URL)) return false;
  int code = http.GET();
  bool ok = false;
  if (code == 200) {
    DeserializationError err = deserializeJson(doc, http.getString());
    ok = !err && doc["ok"].as<bool>();
    if (err) Serial.print("JSON err: "), Serial.println(err.c_str());
  } else {
    Serial.print("HTTP "); Serial.println(code);
  }
  http.end();
  return ok;
}

// copy one parsed account into the cached globals the dashboard draws from
void applyAccount(int i) {
  if (i < 0 || i >= acctCount) return;
  AcctView& v = accts[i];
  cAccount = v.label; accountLabel = v.label;
  cSPct = v.sPct; cSReset = v.sReset; cSActive = v.sActive;
  cWPct = v.wPct; cWReset = v.wReset; cWDays = v.wDays;
}

// pull session/weekly out of one JSON object (an accounts[] entry or the root)
static void fillFrom(JsonVariantConst o, AcctView& v) {
  v.label   = String((const char*)(o["account"] | ""));
  v.active  = o["active"]                  | false;
  v.sPct    = o["session"]["pct"]          | 0;
  v.sReset  = o["session"]["resets_in_min"]| 0;
  v.sActive = o["session"]["active"]       | false;
  v.wPct    = o["weekly"]["pct"]           | 0;
  v.wReset  = o["weekly"]["resets_in_min"] | 0;
  v.wDays   = o["weekly"]["days"]          | 0;
}

void parseAccounts(JsonDocument& doc) {
  acctCount = 0;
  JsonArrayConst arr = doc["accounts"].as<JsonArrayConst>();
  if (!arr.isNull()) {
    for (JsonObjectConst a : arr) {
      if (acctCount >= MAX_ACCOUNTS) break;
      fillFrom(a, accts[acctCount++]);
    }
  }
  if (acctCount == 0) {                         // older bridge / no data: use root keys
    fillFrom(doc.as<JsonVariantConst>(), accts[0]);
    accts[0].active = true;
    acctCount = 1;
  }
  if (acctIdx >= acctCount) acctIdx = 0;        // an account may have dropped off
}

void render(JsonDocument& doc) {
  parseAccounts(doc);
  applyAccount(acctIdx);
  haveData   = true;
  lastOk     = true;
  lastSwitch = millis();                        // dwell a full interval after a refresh
  drawAll(true);
}

void loop() {
  unsigned long now = millis();

  // smooth, time-based animation
  if (now - lastFrame >= FRAME_MS) {
    lastFrame = now;
    animPhase = now * 0.004f;
    if (firstDraw) splashFrame(bootSub);          // boot splash until first data
    else { headerSpark(lastOk); dashWarnings(); } // live spark + maxed-out blink
  }

  // hold on the animated splash until WiFi is up
  if (WiFi.status() != WL_CONNECTED) {
    bootSub = "Connecting WiFi...";
    return;
  }
  if (firstDraw) bootSub = "Fetching usage...";

  // data poll
  if (now - lastPoll >= POLL_MS) {
    lastPoll = now;
    JsonDocument doc;
#if USE_DIRECT_API
#if DEVICE_MANAGES_TOKENS
    bool got = fetchUsageManaged(doc);
#else
    bool got = fetchUsageDirect(doc);
#endif
#else
    bool got = fetchUsage(doc);
#endif
    if (got) {
      render(doc);
      rippleStart = now;                          // "pop" the header spark
    } else {
      if (lastOk && haveData) drawAll(false);     // transition to stale: dim + red header
      lastOk = false;
      if (firstDraw) bootSub = USE_DIRECT_API ? "Waiting for API..." : "Waiting for bridge...";
    }
    // diagnostic: one line per poll so a frozen display is easy to debug over Serial
    Serial.printf("[poll] got=%d accts=%d heap=%u clock=%ld\n",
                  got, acctCount, ESP.getFreeHeap(), (long)time(nullptr));
  }

  // rotate the display through accounts when the bridge reports more than one
  if (!firstDraw && acctCount > 1 && now - lastSwitch >= ACCOUNT_DWELL_MS) {
    lastSwitch = now;
    acctIdx = (acctIdx + 1) % acctCount;
    applyAccount(acctIdx);
    drawAll(lastOk);                              // honors stale dimming
  }
}
