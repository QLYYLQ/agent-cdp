# QStateMachine Class Reference - Qt 6.10.2

> Source: https://doc.qt.io/qt-6/qstatemachine.html

## Overview

QStateMachine implements "a hierarchical finite state machine" based on Statecharts concepts and the State Chart XML (SCXML) algorithm. It manages a collection of states inheriting from QAbstractState and transitions derived from QAbstractTransition, forming a state graph that executes asynchronously through its own event loop.

## Core Architecture

**Inheritance:** QStateMachine extends QState

**CMake:** `find_package(Qt6 REQUIRED COMPONENTS StateMachine)` with `target_link_libraries(mytarget PRIVATE Qt6::StateMachine)`

**Threading:** All functions are reentrant; `postEvent()`, `postDelayedEvent()`, and `cancelDelayedEvent()` are explicitly thread-safe.

## Public Types

**Enumerations:**
- `Error`: NoError, NoInitialStateError, NoDefaultStateInHistoryStateError, NoCommonAncestorForTransitionError, StateMachineChildModeSetToParallelError
- `EventPriority`: NormalPriority, HighPriority

## Properties

| Property | Type | Default | Bindable |
|----------|------|---------|----------|
| animated | bool | true | Yes |
| errorString | QString | — | Yes (read-only) |
| globalRestorePolicy | QState::RestorePolicy | DontRestoreProperties | Yes |
| running | bool | — | No |

## State Management Functions

**Adding/Removing States:**
- `addState(QAbstractState *state)`: Adds top-level state; machine takes ownership
- `removeState(QAbstractState *state)`: Removes state; machine releases ownership
- `setInitialState()`: Required before starting; state entered upon machine startup

**Configuration Query:**
- `configuration()`: Returns maximal consistent set of active states as QSet<QAbstractState*>. Parent states always included if child is active.

## Animation Management

- `addDefaultAnimation(QAbstractAnimation *animation)`: Registers animation for any transition
- `removeDefaultAnimation(QAbstractAnimation *animation)`: Unregisters animation
- `defaultAnimations()`: Returns QList of registered default animations

## Event Processing

**Posting Events:**
- `postEvent(QEvent *event, EventPriority priority)`: Adds event to queue; machine owns event post-processing
- `postDelayedEvent(QEvent *event, int delay)`: Queues event after delay (milliseconds); returns event ID
- `cancelDelayedEvent(int id)`: Cancels delayed event by ID

**Restrictions:** Events post only when machine is running or starting up.

## Execution Control

**Slots:**
- `start()`: Resets configuration, transitions to initial state. Emits `started()` when initial state entered.
- `stop()`: Halts event processing; emits `stopped()`
- `setRunning(bool running)`: Setter for running property

**Signals:**
- `started()`: Emitted upon initial state entry
- `stopped()`: Emitted when machine stops
- `runningChanged(bool running)`: Emitted when running property changes

## Error Handling

Machine seeks error state upon encountering unrecoverable runtime errors. If error state exists, machine enters it; otherwise halts and prints console message.

**Error Types:**
1. `NoInitialStateError` (1): QState with children lacks initial state
2. `NoDefaultStateInHistoryStateError` (2): QHistoryState missing default state
3. `NoCommonAncestorForTransitionError` (3): Transition source/targets in different state trees
4. `StateMachineChildModeSetToParallelError` (4): Machine's childMode set to ParallelStates (illegal)

## Protected Overrides

- `event(QEvent *e)`: Processes internal state machine events
- `eventFilter(QObject *watched, QEvent *event)`: Filters watched object events
- `onEntry(QEvent *event)`: Invokes `start()`
- `onExit(QEvent *event)`: Invokes `stop()` and emits `stopped()`

## Basic Usage Pattern

```cpp
QPushButton button;
QStateMachine machine;
QState *s1 = new QState();
s1->assignProperty(&button, "text", "Click me");

QFinalState *s2 = new QFinalState();
s1->addTransition(&button, &QPushButton::clicked, s2);

machine.addState(s1);
machine.addState(s2);
machine.setInitialState(s1);
machine.start();
```

## Key Constraints

- Machine requires explicit initial state before starting
- State removal during execution discouraged
- ChildMode cannot be set to ParallelStates on machine itself; only individual states support parallel configuration
- Machine executes asynchronously; halts at top-level final state unless explicitly stopped
