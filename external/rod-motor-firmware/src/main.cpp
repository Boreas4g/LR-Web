#include <Arduino.h>
#include <ArduinoJson.h>

#include "rod_motor_config.h"
#include <RodMotor.h>

String receivedData;
StaticJsonDocument<512> jsonCmd;

void printBootInfo() {
  StaticJsonDocument<256> doc;
  doc["T"] = 0;
  doc["ok"] = true;
  doc["project"] = "rod-motor-firmware";
  doc["defaultId"] = ROD_DEFAULT_ID;
  doc["currentId"] = rodMotor.currentId();
  doc["baud"] = ROD_SERVO_BAUD;
  serializeJson(doc, Serial);
  Serial.println();
}

void handleSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\r') {
      continue;
    }
    if (c != '\n') {
      receivedData += c;
      return;
    }

    if (receivedData.length() == 0) {
      return;
    }

    jsonCmd.clear();
    DeserializationError err = deserializeJson(jsonCmd, receivedData);
    receivedData = "";

    if (err != DeserializationError::Ok) {
      StaticJsonDocument<256> res;
      res["T"] = -1;
      res["ok"] = false;
      res["error"] = "json parse failed";
      res["detail"] = err.c_str();
      serializeJson(res, Serial);
      Serial.println();
      return;
    }

    rodMotor.handleCommand(jsonCmd);
  }
}

void setup() {
  Serial.begin(ROD_DEBUG_BAUD);
  delay(500);
  rodMotor.begin();
  printBootInfo();
}

void loop() {
  handleSerial();
}
