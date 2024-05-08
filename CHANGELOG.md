# Changelog

## 2024-05-08
- App CRD values has become object. Modify operator to handle both strings and operators.

## 2023-10-02
- handle exception while creating builder.

## 2022-10-01
- Delete any job when cleaning up application.

## 2022-09-29
- Remove verify=False for all API calls.

## 2022-09-28
- Remove clusterstack update.
- Add apt BP for Drupal.
- Add verify=False for all API calls.

## 2022-08-30
- Update the clusterstack run image sha every 12 hours.

## 2022-08-20

### Changed
- Add build spec while creating app.
- Pass data dict copy to pusher trigger function.

## 2022-08-14

### Changed
- Send failure message to backend on build failure.

## 2022-08-11

### Changed
- node create and delete notifications
- shutdown sends node delete message.

## 2022-08-08

### Changed
- Update tag to 1.0.
- Send HTTP POST request to SB upon deployment changes.
