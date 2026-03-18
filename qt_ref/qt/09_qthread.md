# QThread Class Reference - Qt 6.10.2

> Source: https://doc.qt.io/qt-6/qthread.html

## Overview

QThread is Qt Core's platform-independent threading abstraction. "The QThread class provides a platform-independent way to manage threads."

## Public Enumerations

**Priority enum** defines thread scheduling:
- `IdlePriority` (0) - scheduled only when no other threads run
- `LowestPriority` (1) - less frequent than LowPriority
- `LowPriority` (2) - less frequent than NormalPriority
- `NormalPriority` (3) - default OS priority
- `HighPriority` (4) - more frequent than NormalPriority
- `HighestPriority` (5) - more frequent than HighPriority
- `TimeCriticalPriority` (6) - scheduled as often as possible
- `InheritPriority` (7) - inherits creating thread's priority (default)

**QualityOfService enum** (Qt 6.9+) specifies CPU core preference:
- `Auto` (0) - scheduler decides
- `High` (1) - high-performance CPU core
- `Eco` (2) - energy-efficient CPU core

## Core Execution Methods

**void start(Priority priority = InheritPriority)** [slot]
- Begins thread execution by calling `run()`

**virtual void run()** [protected]
- Entry point after `start()` is called
- Default implementation calls `exec()`
- Returning from this method ends thread execution

**int exec()** [protected]
- Enters event loop; waits until `exit()` called
- Returns exit code passed to `exit()`

## Control & Management

- `void quit()` - Tells event loop to exit with return code 0
- `void exit(int returnCode = 0)` - Instructs event loop to exit
- `void terminate()` - **Dangerous**: Forcibly terminates thread execution
- `void requestInterruption()` - Requests advisory interruption

## State Queries

- `bool isRunning() const` - Thread has started but not finished
- `bool isFinished() const` - Thread returned from `run()`
- `bool isInterruptionRequested() const` - Interruption was requested
- `bool isCurrentThread() const` (Qt 6.8+) - This instance is current thread

## Static Methods

- `QThread* currentThread()` - Currently executing QThread
- `Qt::HANDLE currentThreadId()` - Platform-specific thread handle
- `bool isMainThread()` (Qt 6.8+) - Currently in main thread
- `int idealThreadCount()` - Ideal number of parallel threads
- `void sleep/msleep/usleep(...)` - Thread sleep functions
- `void yieldCurrentThread()` - Yield execution

## Thread Creation (Factory)

```cpp
template <typename Function, typename... Args>
static QThread* create(Function &&f, Args &&... args)
```

Creates new QThread executing function with arguments. Thread is not started; requires explicit `start()`.

## Signals

- `void started()` - Emitted when execution begins
- `void finished()` - Emitted right before thread finishes

## Usage Pattern: Worker Objects (Recommended)

```cpp
Worker *worker = new Worker;
worker->moveToThread(&workerThread);
connect(&workerThread, &QThread::finished, worker, &QObject::deleteLater);
connect(this, &Controller::operate, worker, &Worker::doWork);
```

## Thread Affinity Note

"A QThread instance lives in the old thread that instantiated it, not in the new thread that calls run()." Constructor executes in old thread; `run()` executes in new thread.

## Synchronization

**bool wait(QDeadlineTimer deadline = Forever)**
- Blocks calling thread until associated thread finishes or deadline reached
- Provides POSIX `pthread_join()` equivalent functionality
