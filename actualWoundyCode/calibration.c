#include <gpiod.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <stdint.h>
#include <errno.h>

// Driver headers of VL53L3CX
#include "/home/woundy_team/VL53L3CX_LinuxDriver_1.1.10_bare_1.2.16/driver/vl53Lx/stmvl53lx_if.h"

#define DIR_PIN 21
#define STEP_PIN 20
#define TOTAL_STEPS 24000
#define ACCEL_STEPS 800

#define RAIL_MM 500.0f
#define STEPS_PER_MM (TOTAL_STEPS / RAIL_MM)
#define CALIBRATION_INTERVAL_MM 50
#define CALIBRATION_POINTS ((int)(RAIL_MM / CALIBRATION_INTERVAL_MM) - 1)

int take_median_reading(int fd)
{
    VL53LX_MultiRangingData_t data;
    int readings[5];
    int valid = 0;

    for (int attempt = 0; attempt < 10 && valid < 5; attempt++)
    {
        if (ioctl(fd, VL53LX_IOCTL_MZ_DATA_BLOCKING, &data) >= 0)
        {
            if (data.NumberOfObjectsFound > 0)
            {
                readings[valid++] = data.RangeData[0].RangeMilliMeter;
            }
        }
    }

    if (valid == 0)
        return -1;

    for (int i = 1; i < valid; i++)
    {
        int key = readings[i];
        int j = i - 1;
        while (j >= 0 && readings[j] > key)
        {
            readings[j + 1] = readings[j];
            j--;
        }
        readings[j + 1] = key;
    }

    return readings[valid / 2];
}

void move_steps(struct gpiod_line_request *request, long steps, int *delayTime, int accel)
{
    for (long i = 0; i < steps; i++)
    {
        gpiod_line_request_set_value(request, STEP_PIN, 1);
        usleep(*delayTime);
        gpiod_line_request_set_value(request, STEP_PIN, 0);
        usleep(*delayTime);

        if (accel)
        {
            if (i < ACCEL_STEPS && *delayTime > 150)
                (*delayTime)--;
            if (i > (steps - ACCEL_STEPS) && *delayTime < 1000)
                (*delayTime)++;
        }
    }
}

int main()
{
    printf("--- Woundy Calibration ---\n");
    printf("Moves to each %d mm mark, pauses for you to check, then continues.\n", CALIBRATION_INTERVAL_MM);
    printf("Press Enter to start...\n");
    getchar();

    int fd = open("/dev/stmvl53lx_ranging", O_RDWR);
    if (fd < 0)
    {
        perror("[ToF] Fail open");
        return 1;
    }
    ioctl(fd, VL53LX_IOCTL_START, NULL);

    struct gpiod_chip *chip;
    struct gpiod_line_settings *settings;
    struct gpiod_line_config *line_cfg;
    struct gpiod_request_config *req_cfg;
    struct gpiod_line_request *request;
    unsigned int offsets[2] = {STEP_PIN, DIR_PIN};

    chip = gpiod_chip_open("/dev/gpiochip4");
    settings = gpiod_line_settings_new();
    gpiod_line_settings_set_direction(settings, GPIOD_LINE_DIRECTION_OUTPUT);
    line_cfg = gpiod_line_config_new();
    gpiod_line_config_add_line_settings(line_cfg, offsets, 2, settings);
    req_cfg = gpiod_request_config_new();
    gpiod_request_config_set_consumer(req_cfg, "woundy_calibration");
    request = gpiod_chip_request_lines(chip, req_cfg, line_cfg);

    gpiod_line_request_set_value(request, DIR_PIN, 1);
    int delayTime = 1000;

    long steps_per_interval = (long)(CALIBRATION_INTERVAL_MM * STEPS_PER_MM);
    long total_moved = 0;

    printf("\n%-12s %-14s %s\n", "Rail (mm)", "Step", "ToF (mm)");
    printf("--------------------------------------------\n");

    // Accelerate first
    printf("[NEMA17] Accelerating...\n");
    move_steps(request, ACCEL_STEPS, &delayTime, 1);
    total_moved += ACCEL_STEPS;

    for (int point = 0; point < CALIBRATION_POINTS; point++)
    {
        long target = (long)((point + 1) * CALIBRATION_INTERVAL_MM * STEPS_PER_MM);
        long steps_needed = target - total_moved;

        if (steps_needed > 0)
        {
            move_steps(request, steps_needed, &delayTime, 0);
            total_moved += steps_needed;
        }

        int distance = take_median_reading(fd);
        int rail_pos = (point + 1) * CALIBRATION_INTERVAL_MM;

        if (distance >= 0)
            printf("%-12d %-14ld %d", rail_pos, total_moved, distance);
        else
            printf("%-12d %-14ld FAIL", rail_pos, total_moved);

        printf("  [Enter to continue]");
        getchar();
    }

    // Move remaining steps to end of rail
    long remaining = TOTAL_STEPS - total_moved;
    if (remaining > 0)
    {
        printf("\n[NEMA17] Finishing forward sweep...\n");
        move_steps(request, remaining, &delayTime, 1);
    }

    // Return
    gpiod_line_request_set_value(request, DIR_PIN, 0);
    printf("[NEMA17] Returning...\n");
    delayTime = 1000;
    move_steps(request, TOTAL_STEPS, &delayTime, 1);

    close(fd);

    gpiod_line_request_release(request);
    gpiod_request_config_free(req_cfg);
    gpiod_line_config_free(line_cfg);
    gpiod_line_settings_free(settings);
    gpiod_chip_close(chip);

    printf("\n--- Calibration complete ---\n");
    return 0;
}