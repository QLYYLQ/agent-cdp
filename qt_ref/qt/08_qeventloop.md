# QEventLoop Class Reference - Qt 6.10.2

> Source: https://doc.qt.io/qt-6/qeventloop.html

## Overview
The QEventLoop class provides mechanisms for entering and leaving an event loop. It inherits from QObject and is part of Qt Core.

## Header and Dependencies
```
#include <QEventLoop>
find_package(Qt6 REQUIRED COMPONENTS Core)
target_link_libraries(mytarget PRIVATE Qt6::Core)
QT += core
```

## Public Types

### ProcessEventsFlag Enum
Controls event processing types:
- **AllEvents** (0x00): Processes all events, including deferred deletions
- **ExcludeUserInputEvents** (0x01): Skips button and key presses; events queue for later processing
- **ExcludeSocketNotifiers** (0x02): Skips socket notifications; events queue for later
- **WaitForMoreEvents** (0x04): Blocks until events arrive if none pending

## Constructor and Destructor
- `QEventLoop(QObject *parent = nullptr)` - Creates event loop with optional parent
- `~QEventLoop()` - Virtual destructor

## Core Member Functions

### exec()
`int exec(ProcessEventsFlags flags = AllEvents)`

Enters the main event loop, blocking until `exit()` is called. Returns the exit code passed to `exit()`. Only processes events matching the specified flags.

### processEvents()
Three overloaded versions:
1. `bool processEvents(ProcessEventsFlags flags = AllEvents)` - Handles pending events matching flags; returns true if events were processed
2. `void processEvents(ProcessEventsFlags flags, QDeadlineTimer deadline)` - (Qt 6.7+) Processes events until deadline expires
3. `void processEvents(ProcessEventsFlags flags, int maxTime)` - Processes events for maximum milliseconds specified

### exit() and quit()
- `void exit(int returnCode = 0)` - Slot that terminates event loop with return code
- `void quit()` - Slot equivalent to `exit(0)`

### Status Functions
- `bool isRunning() const` - Returns true if loop is currently active
- `void wakeUp()` - Awakens the event loop

### Reimplemented Functions
- `bool event(QEvent *event) override` - Handles event dispatch

## Usage Pattern

"At any time, you can create a QEventLoop object and call exec() on it to start a local event loop. From within the event loop, calling exit() will force exec() to return."

## Typical Application
Event loops process system events and dispatch them to application widgets. Modal dialogs like QMessageBox operate their own local event loops before the main application loop starts.
