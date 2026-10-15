#ifndef ROD_MOTOR_CONFIG_H
#define ROD_MOTOR_CONFIG_H

#include <Arduino.h>

// Serial ports.
#define ROD_DEBUG_BAUD 921600
#define ROD_SERVO_BAUD 1000000

// ESP32 UART pins for the ST3215/SCServo TTL bus.
#define ROD_SERVO_RXD 18
#define ROD_SERVO_TXD 19

// Rod motor defaults.
#define ROD_DEFAULT_ID 16
#define ROD_MAX_SERVO_ID 253

#define ROD_RAW_MIN_POS 0
#define ROD_RAW_MAX_POS 4095
#define ROD_MIDDLE_POS 2047
#define ROD_POS_RANGE 4096

// rad = 0 maps to ROD_ZERO_POS. Direction is -1 to match the current arm firmware.
#define ROD_ZERO_POS 2047
#define ROD_POS_DIRECTION -1

#define ROD_DEFAULT_SPEED 600
#define ROD_DEFAULT_ACC 20
#define ROD_DEFAULT_TORQUE 1000
#define ROD_TORQUE_MIN 50
#define ROD_TORQUE_MAX 1000

// ST3215 PID register addresses used by the current firmware.
#define ROD_ST_PID_P_ADDR 21
#define ROD_ST_PID_D_ADDR 22
#define ROD_ST_PID_I_ADDR 23
#define ROD_ST_PID_DEFAULT_P 16
#define ROD_ST_PID_DEFAULT_I 0
#define ROD_ST_PID_DEFAULT_D 0

#endif
