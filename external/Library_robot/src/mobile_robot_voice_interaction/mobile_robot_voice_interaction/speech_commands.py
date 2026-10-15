"""Shared speech-command definitions for a command-based voice module."""

COMMAND_CODE_TO_NAME = {
    0: "idle_or_stop",
    2: "stop",
    4: "go_ahead",
    5: "back_off",
    6: "turn_left",
    7: "turn_right",
    10: "close_light",
    11: "red_light",
    12: "green_light",
    13: "blue_light",
    14: "yellow_light",
    15: "water_lamps",
    16: "gradient_light",
    17: "breathing_light",
    18: "display_electricity",
    19: "goal_one",
    20: "goal_two",
    21: "goal_three",
    32: "goal_four",
    33: "goal_origin",
}

COMMAND_NAME_TO_CODE = {name: code for code, name in COMMAND_CODE_TO_NAME.items()}

COMMAND_ALIASES = {
    "idle": 0,
    "stop_car": 2,
    "forward": 4,
    "backward": 5,
    "left": 6,
    "right": 7,
    "lights_off": 10,
    "light_red": 11,
    "light_green": 12,
    "light_blue": 13,
    "light_yellow": 14,
    "water_lamp": 15,
    "gradient": 16,
    "breathing": 17,
    "battery": 18,
    "goal_1": 19,
    "goal_2": 20,
    "goal_3": 21,
    "goal_4": 32,
    "origin": 33,
}


def normalize_command_name(value):
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def command_name_for_code(code):
    return COMMAND_CODE_TO_NAME.get(int(code), f"unknown_{int(code)}")


def command_code_for_name(value):
    key = normalize_command_name(value)
    if key in COMMAND_NAME_TO_CODE:
        return COMMAND_NAME_TO_CODE[key]
    if key in COMMAND_ALIASES:
        return COMMAND_ALIASES[key]
    raise KeyError(value)
