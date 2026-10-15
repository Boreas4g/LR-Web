#include "RodMotor.h"

RodMotorController rodMotor;

RodMotorController::RodMotorController()
  : feedback_{false, 0, 0, 0, 0, 0, 0, 0},
    online_(false),
    requestedTorque_(ROD_DEFAULT_TORQUE),
    torqueWarned_(false),
    positionModeActive_(true),
    currentId_(ROD_DEFAULT_ID) {}

void RodMotorController::begin() {
  Serial1.begin(ROD_SERVO_BAUD, SERIAL_8N1, ROD_SERVO_RXD, ROD_SERVO_TXD);
  bus_.pSerial = &Serial1;
  delay(20);
}

bool RodMotorController::takeBus(const char* owner) {
  (void)owner;
  return true;
}

void RodMotorController::releaseBus(uint32_t quietMs) {
  delay(quietMs);
}

void RodMotorController::pauseArmConstant(uint32_t pauseMs) {
  (void)pauseMs;
}

void RodMotorController::storeFeedback(const RodMotorFeedback& feedback) {
  feedback_ = feedback;
  online_ = feedback.status;
}

int RodMotorController::constrainRaw(int inputPos) {
  return constrain(inputPos, ROD_RAW_MIN_POS, ROD_RAW_MAX_POS);
}

int RodMotorController::radToRaw(double rad) {
  double pos = ROD_ZERO_POS + ROD_POS_DIRECTION * (rad / (2.0 * M_PI)) * ROD_POS_RANGE;
  return constrainRaw((int)round(pos));
}

double RodMotorController::rawToRad(int raw) {
  if (ROD_POS_DIRECTION == 0 || raw < ROD_RAW_MIN_POS || raw > ROD_RAW_MAX_POS) {
    return 0;
  }
  return (raw - ROD_ZERO_POS) * (2.0 * M_PI) / (ROD_POS_DIRECTION * ROD_POS_RANGE);
}

bool RodMotorController::readRaw(byte id, RodMotorFeedback& feedback, bool quiet) {
  feedback.status = false;
  if (!takeBus("rod feedback")) {
    if (id == currentId_) {
      storeFeedback(feedback);
    }
    return false;
  }

  bool ok = bus_.FeedBack(id) != -1;
  if (ok) {
    feedback.status = true;
    feedback.pos = bus_.ReadPos(-1);
    feedback.speed = bus_.ReadSpeed(-1);
    feedback.load = bus_.ReadLoad(-1);
    feedback.voltage = bus_.ReadVoltage(-1);
    feedback.current = bus_.ReadCurrent(-1);
    feedback.temper = bus_.ReadTemper(-1);
    feedback.mode = bus_.ReadMode(id);
  }
  releaseBus(4);

  if (id == currentId_) {
    storeFeedback(feedback);
  }
  if (!ok && !quiet) {
    Serial.print("rod servo ID ");
    Serial.print(id);
    Serial.println(" feedback failed");
  }
  return ok;
}

bool RodMotorController::readRad(byte id, RodMotorFeedback& feedback, bool quiet) {
  return readRaw(id, feedback, quiet);
}

bool RodMotorController::moveRaw(byte id, int pos, uint16_t spd, uint8_t acc) {
  int rawPos = constrainRaw(pos);
  uint16_t rawSpeed = spd == 0 ? ROD_DEFAULT_SPEED : spd;
  uint8_t rawAcc = acc == 0 ? ROD_DEFAULT_ACC : acc;

  if (id == currentId_ && !positionModeActive_) {
    bus_.unLockEprom(id);
    bus_.writeByte(id, SMS_STS_MODE, 0);
    bus_.LockEprom(id);
    positionModeActive_ = true;
  }

  if (!takeBus("rod raw move")) {
    return false;
  }
  bus_.WritePosEx(id, rawPos, rawSpeed, rawAcc);
  pauseArmConstant();
  releaseBus(4);
  return true;
}

bool RodMotorController::moveRad(byte id, double rad, uint16_t spd, uint8_t acc) {
  return moveRaw(id, radToRaw(rad), spd, acc);
}

bool RodMotorController::setPID(byte id, byte p, byte i, byte d) {
  RodMotorFeedback feedback;
  if (!readRaw(id, feedback, true)) {
    return false;
  }
  if (!takeBus("rod pid")) {
    return false;
  }
  bus_.unLockEprom(id);
  bus_.writeByte(id, ROD_ST_PID_P_ADDR, p);
  bus_.writeByte(id, ROD_ST_PID_D_ADDR, d);
  bus_.writeByte(id, ROD_ST_PID_I_ADDR, i);
  bus_.LockEprom(id);
  releaseBus(4);
  return true;
}

bool RodMotorController::resetPID(byte id) {
  return setPID(id, ROD_ST_PID_DEFAULT_P, ROD_ST_PID_DEFAULT_I, ROD_ST_PID_DEFAULT_D);
}

bool RodMotorController::changeID(byte oldId, byte newId) {
  if (newId == 0 || newId > ROD_MAX_SERVO_ID) {
    return false;
  }

  RodMotorFeedback feedback;
  if (!readRaw(oldId, feedback, true)) {
    return false;
  }
  if (!takeBus("rod id")) {
    return false;
  }

  bus_.unLockEprom(oldId);
  bus_.writeByte(oldId, SMS_STS_ID, newId);
  bus_.LockEprom(newId);
  releaseBus(80);

  RodMotorFeedback newFeedback;
  bool newOnline = readRaw(newId, newFeedback, true);
  if (newOnline) {
    currentId_ = newId;
    storeFeedback(newFeedback);
  }
  return newOnline;
}

bool RodMotorController::setMiddle(byte id) {
  if (!takeBus("rod middle")) {
    return false;
  }
  bus_.CalibrationOfs(id);
  releaseBus(4);
  return true;
}

void RodMotorController::writeJsonResponse(const JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.println();
}

void RodMotorController::handleCommand(JsonDocument& doc) {
  int cmd = doc["T"] | -1;
  byte id = doc["id"] | currentId_;

  StaticJsonDocument<512> res;
  res["T"] = cmd * 10 + 1;
  res["target"] = "rod";
  res["id"] = id;

  switch (cmd) {
    case CMD_ROD_RAW_FEEDBACK: {
      RodMotorFeedback feedback;
      bool ok = readRaw(id, feedback, true);
      res["ok"] = ok;
      res["pos"] = feedback.pos;
      res["rad"] = rawToRad(feedback.pos);
      res["status"] = feedback.status;
      res["speed"] = feedback.speed;
      res["load"] = feedback.load;
      res["mode"] = feedback.mode;
      break;
    }
    case CMD_ROD_RAW_CTRL: {
      int pos = constrainRaw(doc["pos"] | ROD_MIDDLE_POS);
      uint16_t spd = doc["spd"] | ROD_DEFAULT_SPEED;
      uint8_t acc = doc["acc"] | ROD_DEFAULT_ACC;
      bool ok = moveRaw(id, pos, spd, acc);
      res["ok"] = ok;
      res["pos"] = pos;
      res["rad"] = rawToRad(pos);
      res["spd"] = spd;
      res["acc"] = acc;
      break;
    }
    case CMD_ROD_RAD_FEEDBACK: {
      RodMotorFeedback feedback;
      bool ok = readRad(id, feedback, true);
      res["ok"] = ok;
      res["rad"] = rawToRad(feedback.pos);
      res["angle"] = rawToRad(feedback.pos) * 180.0 / M_PI;
      res["pos"] = feedback.pos;
      res["status"] = feedback.status;
      res["speed"] = feedback.speed;
      res["load"] = feedback.load;
      res["voltage"] = feedback.voltage;
      res["current"] = feedback.current;
      res["temper"] = feedback.temper;
      res["mode"] = feedback.mode;
      break;
    }
    case CMD_ROD_RAD_CTRL: {
      double rad = doc["rad"] | 0.0;
      uint16_t spd = doc["spd"] | ROD_DEFAULT_SPEED;
      uint8_t acc = doc["acc"] | ROD_DEFAULT_ACC;
      bool ok = moveRad(id, rad, spd, acc);
      res["ok"] = ok;
      res["rad"] = rad;
      res["angle"] = rad * 180.0 / M_PI;
      res["pos"] = radToRaw(rad);
      res["spd"] = spd;
      res["acc"] = acc;
      break;
    }
    case CMD_ROD_SET_PID: {
      byte p = doc["p"] | ROD_ST_PID_DEFAULT_P;
      byte i = doc["i"] | ROD_ST_PID_DEFAULT_I;
      byte d = doc["d"] | ROD_ST_PID_DEFAULT_D;
      bool ok = setPID(id, p, i, d);
      res["ok"] = ok;
      res["p"] = p;
      res["i"] = i;
      res["d"] = d;
      break;
    }
    case CMD_ROD_RESET_PID: {
      bool ok = resetPID(id);
      res["ok"] = ok;
      res["p"] = ROD_ST_PID_DEFAULT_P;
      res["i"] = ROD_ST_PID_DEFAULT_I;
      res["d"] = ROD_ST_PID_DEFAULT_D;
      break;
    }
    case CMD_ROD_SET_ID: {
      if (!doc.containsKey("new")) {
        res["ok"] = false;
        res["error"] = "missing new id";
        break;
      }
      byte oldId = currentId_;
      if (doc.containsKey("raw")) {
        oldId = doc["raw"].as<byte>();
      } else if (doc.containsKey("old")) {
        oldId = doc["old"].as<byte>();
      } else if (doc.containsKey("id")) {
        oldId = doc["id"].as<byte>();
      }
      byte newId = doc["new"].as<byte>();
      bool ok = changeID(oldId, newId);
      res["ok"] = ok;
      res["old"] = oldId;
      res["new"] = newId;
      res["currentId"] = currentId_;
      res["newOnline"] = ok;
      break;
    }
    case CMD_ROD_SET_MIDDLE: {
      bool ok = setMiddle(id);
      res["ok"] = ok;
      break;
    }
    default:
      res["T"] = -1;
      res["ok"] = false;
      res["error"] = "unknown command";
      break;
  }

  writeJsonResponse(res);
}
