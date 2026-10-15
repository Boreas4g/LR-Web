#ifndef ROD_MOTOR_H
#define ROD_MOTOR_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <SCServo.h>
#include <math.h>

#include "../../include/rod_motor_config.h"
#include "../../include/rod_motor_commands.h"

struct RodMotorFeedback {
  bool status;
  int pos;
  int speed;
  int load;
  float voltage;
  float current;
  float temper;
  byte mode;
};

class RodMotorController {
 public:
  RodMotorController();

  void begin();
  bool readRaw(byte id, RodMotorFeedback& feedback, bool quiet = false);
  bool readRad(byte id, RodMotorFeedback& feedback, bool quiet = false);
  bool moveRaw(byte id, int pos, uint16_t spd, uint8_t acc);
  bool moveRad(byte id, double rad, uint16_t spd, uint8_t acc);
  bool setPID(byte id, byte p, byte i, byte d);
  bool resetPID(byte id);
  bool changeID(byte oldId, byte newId);
  bool setMiddle(byte id);
  void writeJsonResponse(const JsonDocument& doc);
  void handleCommand(JsonDocument& doc);

  byte currentId() const { return currentId_; }
  void setCurrentId(byte id) { currentId_ = id; }

  static int constrainRaw(int inputPos);
  static int radToRaw(double rad);
  static double rawToRad(int raw);

 private:
  void storeFeedback(const RodMotorFeedback& feedback);
  bool takeBus(const char* owner);
  void releaseBus(uint32_t quietMs);
  void pauseArmConstant(uint32_t pauseMs = 350);

  SMS_STS bus_;
  RodMotorFeedback feedback_;
  bool online_;
  int requestedTorque_;
  bool torqueWarned_;
  bool positionModeActive_;
  byte currentId_;
};

extern RodMotorController rodMotor;

#endif
