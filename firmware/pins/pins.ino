/*
  house-pins 2 — one generic sketch for any Uno/Nano (spec 04).

  The house configures the board at runtime over a readable text protocol:
  one command per line at 115200 baud, exactly one reply per command.
  Flash it once; a robot is then a config file of parts, never a new sketch.

    PING | INFO | MODE <pin> <out|pwm|in|pullup|analog|servo> | W <pin> <v>
    R <pin> | TONE <pin> <hz> <ms> | NOTONE <pin> | WATCH <pin> | RESET

  Unsolicited: HELLO house-pins 2 <board> after every reset, E <pin> <0|1>
  when a watched pin changes (debounced 30 ms). A0..A5 are pins 14..19;
  pins 0 and 1 are the serial link and are refused.
*/
#include <Servo.h>

#if defined(ARDUINO_AVR_MEGA2560)
  #define BOARD_NAME "mega"
#elif defined(ARDUINO_AVR_UNO)
  #define BOARD_NAME "uno"
#elif defined(ARDUINO_AVR_NANO)
  #define BOARD_NAME "nano"
#else
  #define BOARD_NAME "arduino"
#endif

static const uint8_t MODE_NONE = 0, MODE_IN = 1, MODE_OUT = 2, MODE_PWM = 3,
                     MODE_PULLUP = 4, MODE_ANALOG = 5, MODE_SERVO = 6;
static const int MAX_SERVO_COUNT = 4, MAX_WATCH_COUNT = 8;

static uint8_t pinModes[NUM_DIGITAL_PINS];
static Servo servos[MAX_SERVO_COUNT];
static int servoPins[MAX_SERVO_COUNT];
static uint8_t watchPins[MAX_WATCH_COUNT];
static uint8_t watchVals[MAX_WATCH_COUNT];
static unsigned long watchSince[MAX_WATCH_COUNT];
static int watchCount = 0;
static int tonePin = -1;

static bool badPin(int pin) { return pin < 2 || pin >= NUM_DIGITAL_PINS; }

static int servoSlot(int pin) {
  for (int i = 0; i < MAX_SERVO_COUNT; i++) if (servoPins[i] == pin) return i;
  return -1;
}

static void detachServo(int pin) {
  int slot = servoSlot(pin);
  if (slot >= 0) { servos[slot].detach(); servoPins[slot] = -1; }
}

static void setMode(int pin, uint8_t mode) {
  if (pinModes[pin] == MODE_SERVO && mode != MODE_SERVO) detachServo(pin);
  pinModes[pin] = mode;
  switch (mode) {
    case MODE_OUT: pinMode(pin, OUTPUT); break;
    case MODE_PWM: pinMode(pin, OUTPUT); break;
    case MODE_IN: pinMode(pin, INPUT); break;
    case MODE_PULLUP: pinMode(pin, INPUT_PULLUP); break;
    case MODE_ANALOG: pinMode(pin, INPUT); break;
    case MODE_SERVO: break;  // attached by the MODE handler
    default: pinMode(pin, INPUT); break;
  }
}

static void doReset() {
  if (tonePin >= 0) { noTone(tonePin); tonePin = -1; }
  for (int i = 0; i < MAX_SERVO_COUNT; i++)
    if (servoPins[i] >= 0) { servos[i].detach(); servoPins[i] = -1; }
  watchCount = 0;
  for (int p = 2; p < NUM_DIGITAL_PINS; p++) { pinModes[p] = MODE_NONE; pinMode(p, INPUT); }
}

static void err(const char *msg) { Serial.print(F("ERR ")); Serial.println(msg); }
static void ok() { Serial.println(F("OK")); }

// Split a line into up to 5 whitespace-separated tokens. Returns the count.
static int tokenize(char *line, char *tok[], int maxTok) {
  int n = 0;
  char *p = line;
  while (*p && n < maxTok) {
    while (*p == ' ' || *p == '\t') p++;
    if (!*p) break;
    tok[n++] = p;
    while (*p && *p != ' ' && *p != '\t') p++;
    if (*p) *p++ = '\0';
  }
  return n;
}

static void handleLine(char *line) {
  char *tok[5];
  int n = tokenize(line, tok, 5);
  if (n == 0) return;
  String cmd = tok[0];

  if (cmd == "PING") { Serial.println(F("PONG")); return; }
  if (cmd == "INFO") {
    Serial.print(F("INFO house-pins 2 ")); Serial.println(F(BOARD_NAME)); return;
  }
  if (cmd == "RESET") { doReset(); ok(); return; }

  if (cmd == "MODE" && n >= 3) {
    int pin = atoi(tok[1]);
    if (badPin(pin)) { err("bad pin"); return; }
    String mode = tok[2];
    if (mode == "analog" && pin < A0) { err("not an analog pin"); return; }
    if (mode == "servo") {
      int slot = servoSlot(pin);
      if (slot < 0) {
        for (int i = 0; i < MAX_SERVO_COUNT; i++) if (servoPins[i] < 0) { slot = i; break; }
        if (slot < 0) { err("too many servos"); return; }
        servoPins[slot] = pin;
        servos[slot].attach(pin);
      }
      pinModes[pin] = MODE_SERVO;
      ok(); return;
    }
    uint8_t m;
    if (mode == "out") m = MODE_OUT;
    else if (mode == "pwm") m = MODE_PWM;
    else if (mode == "in") m = MODE_IN;
    else if (mode == "pullup") m = MODE_PULLUP;
    else if (mode == "analog") m = MODE_ANALOG;
    else { err("unknown mode"); return; }
    setMode(pin, m);
    ok(); return;
  }

  if (cmd == "W" && n >= 3) {
    int pin = atoi(tok[1]);
    if (badPin(pin)) { err("bad pin"); return; }
    uint8_t m = pinModes[pin];
    if (m != MODE_OUT && m != MODE_PWM && m != MODE_SERVO) { err("pin is not an output"); return; }
    int v = atoi(tok[2]);
    if (m == MODE_SERVO) {
      int slot = servoSlot(pin);
      if (slot >= 0) servos[slot].write(constrain(v, 0, 180));
    } else if (m == MODE_PWM) {
      analogWrite(pin, constrain(v, 0, 255));
    } else {
      digitalWrite(pin, v != 0 ? HIGH : LOW);
    }
    ok(); return;
  }

  if (cmd == "R" && n >= 2) {
    int pin = atoi(tok[1]);
    if (badPin(pin)) { err("bad pin"); return; }
    long value;
    if (pinModes[pin] == MODE_ANALOG) {
      // Mean over 20 ms of continuous sampling: two full 100 Hz flicker
      // cycles, so a lamp-lit room reads steady instead of anywhere.
      unsigned long start = millis();
      long sum = 0, count = 0;
      while (millis() - start < 20) { sum += analogRead(pin); count++; }
      value = count ? sum / count : analogRead(pin);
    } else {
      value = digitalRead(pin);
    }
    Serial.print(F("V ")); Serial.print(pin); Serial.print(' '); Serial.println(value);
    return;
  }

  if (cmd == "TONE" && n >= 4) {
    int pin = atoi(tok[1]);
    if (badPin(pin)) { err("bad pin"); return; }
    if (tonePin >= 0 && tonePin != pin) noTone(tonePin);
    tonePin = pin;
    tone(pin, (unsigned int)atoi(tok[2]), (unsigned long)atol(tok[3]));
    ok(); return;
  }

  if (cmd == "NOTONE" && n >= 2) {
    int pin = atoi(tok[1]);
    if (badPin(pin)) { err("bad pin"); return; }
    noTone(pin);
    if (tonePin == pin) tonePin = -1;
    ok(); return;
  }

  if (cmd == "WATCH" && n >= 2) {
    int pin = atoi(tok[1]);
    if (badPin(pin)) { err("bad pin"); return; }
    for (int i = 0; i < watchCount; i++) if (watchPins[i] == pin) { ok(); return; }
    if (watchCount >= MAX_WATCH_COUNT) { err("too many watches"); return; }
    watchPins[watchCount] = (uint8_t)pin;
    watchVals[watchCount] = (uint8_t)digitalRead(pin);
    watchSince[watchCount] = millis();
    watchCount++;
    ok(); return;
  }

  Serial.print(F("ERR unknown command: ")); Serial.println(line);
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < MAX_SERVO_COUNT; i++) servoPins[i] = -1;
  for (int p = 0; p < NUM_DIGITAL_PINS; p++) pinModes[p] = MODE_NONE;
  Serial.print(F("HELLO house-pins 2 ")); Serial.println(F(BOARD_NAME));
}

void loop() {
  static char buf[48];
  static uint8_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (len > 0) { buf[len] = '\0'; handleLine(buf); len = 0; }
    } else if (len < 47) {
      buf[len++] = c;
    }  // characters beyond 47 are dropped
  }
  unsigned long now = millis();
  for (int i = 0; i < watchCount; i++) {
    uint8_t v = (uint8_t)digitalRead(watchPins[i]);
    if (v == watchVals[i]) {
      watchSince[i] = now;  // refreshed every pass while it stays the same
    } else if (now - watchSince[i] > 30) {
      watchVals[i] = v;
      watchSince[i] = now;
      Serial.print(F("E ")); Serial.print(watchPins[i]); Serial.print(' '); Serial.println(v);
    }
  }
}
