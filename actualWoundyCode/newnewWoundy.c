#include <gpiod.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <stdint.h>
#include <errno.h>
#include <curl/curl.h>
#include <pthread.h>
#include <time.h>

// Driver headers of VL53L3CX
#include "/home/woundy_team/VL53L3CX_LinuxDriver_1.1.10_bare_1.2.16/driver/vl53Lx/stmvl53lx_if.h"

#define DIR_PIN 21
#define STEP_PIN 20
#define TOTAL_STEPS 24000
#define ACCEL_STEPS 800

// IoT settings
#define TS_API_KEY "2QJ8KG8LIADLBOVC"
#define TS_CHANNEL_ID "3256717"

volatile int is_constant_speed = 0;
volatile long current_motor_step = 0;
volatile int motor_finished = 0;

volatile long scan_start_step = 0;
volatile long scan_end_step = 0;

volatile int n_measurements = 0;

// Bulk Upload
void bulk_push_to_iot(int *measurements, int count)
{
    CURL *curl;
    CURLcode res;
    char url[256];
    char temp[128];

    printf("\n[IoT-Bulk] Creating JSON for %d measurements...\n", count);

    sprintf(url, "https://api.thingspeak.com/channels/%s/bulk_update.json", TS_CHANNEL_ID);

    int payload_size = 128 + count * 70;
    char *payload = malloc(payload_size);
    if (!payload)
    {
        fprintf(stderr, "[IoT-Bulk] malloc failed\n");
        return;
    }

    sprintf(payload, "{\"write_api_key\":\"%s\",\"updates\":[", TS_API_KEY);

    time_t current_time = time(NULL);

    for (int i = 0; i < count; i++)
    {
        struct tm *tm_info = gmtime(&current_time);
        char time_buf[30];
        strftime(time_buf, 30, "%Y-%m-%dT%H:%M:%SZ", tm_info);

        sprintf(temp, "{\"created_at\":\"%s\",\"field1\":%d}", time_buf, measurements[i]);
        strcat(payload, temp);

        if (i < count - 1)
        {
            strcat(payload, ",");
        }
        current_time++;
    }
    strcat(payload, "]}");

    curl = curl_easy_init();
    if (curl)
    {
        struct curl_slist *headers = NULL;
        headers = curl_slist_append(headers, "Content-Type: application/json");

        curl_easy_setopt(curl, CURLOPT_URL, url);
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload);

        printf("[IoT-Bulk] Uploading to ThingSpeak...\n");
        res = curl_easy_perform(curl);

        if (res != CURLE_OK)
        {
            fprintf(stderr, "\n[IoT-Bulk] Fail cURL: %s\n", curl_easy_strerror(res));
        }
        else
        {
            printf("\n[IoT-Bulk] Success Bulk Upload!\n");
        }

        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
    }

    free(payload);
}

// Sensor & IoT Thread
void *sensor_iot_thread(void *arg)
{
    int fd = open("/dev/stmvl53lx_ranging", O_RDWR);
    if (fd < 0)
    {
        perror("[ToF] Fail open");
        return NULL;
    }

    ioctl(fd, VL53LX_IOCTL_START, NULL);

    int *measurements = malloc((n_measurements + 7) * sizeof(int));
    if (!measurements)
    {
        perror("[ToF] malloc failed");
        close(fd);
        return NULL;
    }
    int count = 0;
    int meas_taken = 0;

    long scan_steps = scan_end_step - scan_start_step;
    long steps_per_meas = scan_steps / n_measurements;
    if (steps_per_meas < 1)
        steps_per_meas = 1;
    long target_step = scan_start_step;

    VL53LX_MultiRangingData_t data;

    // Add start separators (-17)
    for (int i = 0; i < 3; i++)
    {
        measurements[count++] = -17;
    }

    printf("[ToF] Wait for constant speed...\n");

    while (!is_constant_speed && !motor_finished)
    {
        usleep(10000);
    }

    if (is_constant_speed)
    {
        printf("[ToF] Measuring %d times...\n", n_measurements);
        while (is_constant_speed && meas_taken < n_measurements)
        {
            if (current_motor_step >= target_step)
            {
                if (ioctl(fd, VL53LX_IOCTL_MZ_DATA_BLOCKING, &data) >= 0)
                {
                    if (data.NumberOfObjectsFound > 0)
                    {
                        measurements[count++] = data.RangeData[0].RangeMilliMeter;
                        meas_taken++;
                        target_step += steps_per_meas;
                    }
                }
            }
            else
            {
                usleep(1000);
            }
        }
        printf("[ToF] %d measurements taken :)\n", meas_taken);
    }

    while (!motor_finished)
    {
        usleep(100000);
    }

    // Append actual measurement count
    measurements[count++] = meas_taken;

    // Add end separators (-42)
    for (int i = 0; i < 3; i++)
    {
        measurements[count++] = -42;
    }
    close(fd);

    if (count > 0)
    {
        bulk_push_to_iot(measurements, count);
    }

    free(measurements);
    return NULL;
}

int main()
{
    curl_global_init(CURL_GLOBAL_ALL);

    printf("--- Woundy time ---\n");
    printf("Press Enter to wake up Woundy...\n");
    getchar();

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
    gpiod_request_config_set_consumer(req_cfg, "woundy_motor");
    request = gpiod_chip_request_lines(chip, req_cfg, line_cfg);

    float scan_start_mm, scan_end_mm;
    printf("\nEnter scan start position on rail (mm): ");
    scanf("%f", &scan_start_mm);
    printf("Enter scan end position on rail (mm): ");
    scanf("%f", &scan_end_mm);

    if (scan_start_mm < 0)
        scan_start_mm = 0;
    if (scan_end_mm > 500)
        scan_end_mm = 500;
    if (scan_start_mm >= scan_end_mm)
    {
        printf("Invalid range. Exiting.\n");
        gpiod_line_request_release(request);
        gpiod_request_config_free(req_cfg);
        gpiod_line_config_free(line_cfg);
        gpiod_line_settings_free(settings);
        gpiod_chip_close(chip);
        curl_global_cleanup();
        return 1;
    }

    float steps_per_mm = (float)TOTAL_STEPS / 500.0f;
    scan_start_step = (long)(scan_start_mm * steps_per_mm);
    scan_end_step = (long)(scan_end_mm * steps_per_mm);

    // Clamp scan region to constant-speed window
    if (scan_start_step < ACCEL_STEPS)
        scan_start_step = ACCEL_STEPS;
    if (scan_end_step > TOTAL_STEPS - ACCEL_STEPS)
        scan_end_step = TOTAL_STEPS - ACCEL_STEPS;

    printf("[Scan] Region: %.1f-%.1f mm -> steps %ld-%ld\n",
           scan_start_mm, scan_end_mm, scan_start_step, scan_end_step);

    // Consume leftover newline from scanf
    while (getchar() != '\n')
        ;

    float scan_length_mm = scan_end_mm - scan_start_mm;
    n_measurements = (int)(scan_length_mm * 2);
    if (n_measurements < 1)
        n_measurements = 1;

    printf("[Scan] %d measurements (2/mm over %.1f mm)\n", n_measurements, scan_length_mm);

    printf("\nWoundy is ready to start scanning! Press Enter to start...");
    getchar();

    // Spawn sensor thread AFTER all setup is complete
    pthread_t sensor_thread;
    if (pthread_create(&sensor_thread, NULL, sensor_iot_thread, NULL) != 0)
    {
        gpiod_line_request_release(request);
        gpiod_request_config_free(req_cfg);
        gpiod_line_config_free(line_cfg);
        gpiod_line_settings_free(settings);
        gpiod_chip_close(chip);
        curl_global_cleanup();
        return 1;
    }

    gpiod_line_request_set_value(request, DIR_PIN, 1);
    int delayTime = 1000;

    // 7.5 right
    printf("\n[NEMA17] Starting...\n");
    for (long i = 0; i < TOTAL_STEPS; i++)
    {
        current_motor_step = i;

        if (i == ACCEL_STEPS)
            is_constant_speed = 1;
        if (i == TOTAL_STEPS - ACCEL_STEPS)
            is_constant_speed = 0;

        gpiod_line_request_set_value(request, STEP_PIN, 1);
        usleep(delayTime);
        gpiod_line_request_set_value(request, STEP_PIN, 0);
        usleep(delayTime);

        if (i < ACCEL_STEPS && delayTime > 150)
            delayTime--;
        if (i > (TOTAL_STEPS - ACCEL_STEPS) && delayTime < 1000)
            delayTime++;
    }

    gpiod_line_request_set_value(request, DIR_PIN, 0);

    // 7.5 left
    printf("\n[NEMA17] Returning...\n");
    for (long i = 0; i < TOTAL_STEPS; i++)
    {
        if (i == TOTAL_STEPS - ACCEL_STEPS)
            is_constant_speed = 0;

        gpiod_line_request_set_value(request, STEP_PIN, 1);
        usleep(delayTime);
        gpiod_line_request_set_value(request, STEP_PIN, 0);
        usleep(delayTime);

        if (i < ACCEL_STEPS && delayTime > 150)
            delayTime--;
        if (i > (TOTAL_STEPS - ACCEL_STEPS) && delayTime < 1000)
            delayTime++;
    }

    motor_finished = 1;
    printf("[NEMA17] Done.\n");

    pthread_join(sensor_thread, NULL);

    gpiod_line_request_release(request);
    gpiod_request_config_free(req_cfg);
    gpiod_line_config_free(line_cfg);
    gpiod_line_settings_free(settings);
    gpiod_chip_close(chip);
    curl_global_cleanup();

    printf("\n--- Woundy goes to sleep. Bye! ---\n");
    return 0;
}