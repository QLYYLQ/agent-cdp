# QEvent Class Reference - Qt 6.10.2

> Source: https://doc.qt.io/qt-6/qevent.html

## Overview
"The QEvent class is the base class of all event classes. Event objects contain event parameters." It serves as the foundation for Qt's event system, which is processed through QCoreApplication's main event loop.

## Header and Dependencies
```
#include <QEvent>
find_package(Qt6 REQUIRED COMPONENTS Core)
target_link_libraries(mytarget PRIVATE Qt6::Core)
QT += core
```

## Key Public Functions

**Constructors & Destructors:**
- `QEvent(QEvent::Type type)` - Explicit constructor taking event type
- `~QEvent()` - Virtual destructor

**Event Handling Methods:**
- `void accept()` - Sets the accept flag to indicate the receiver wants the event
- `void ignore()` - Clears the accept flag
- `bool isAccepted() const` - Returns current accept state
- `virtual void setAccepted(bool accepted)` - Sets accept state

**Event Properties:**
- `QEvent::Type type() const` - Returns the event's type
- `bool spontaneous() const` - Returns true if event originated from the system

**Additional Methods (Qt 6.0+):**
- `virtual QEvent* clone() const` - Creates an identical copy
- `bool isInputEvent() const` - Checks if event is QInputEvent or subclass
- `bool isPointerEvent() const` - Checks if event is QPointerEvent or subclass
- `bool isSinglePointEvent() const` - Checks if event is QSinglePointEvent subclass

**Static Member:**
- `static int registerEventType(int hint = -1)` - Registers custom event type

## QEvent::Type Enum

The enum defines 100+ event types including:

**Core Events:** Timer (1), MouseButtonPress (2), MouseButtonRelease (3), MouseMove (5), KeyPress (6), KeyRelease (7)

**Widget Events:** Paint (12), Resize (14), Move (13), Show (17), Hide (18), Close (19), FocusIn (8), FocusOut (9), Enter (10), Leave (11)

**Drag & Drop:** DragEnter (60), DragMove (61), DragLeave (62), Drop (63)

**Graphics Scene Events:** GraphicsSceneMousePress (156), GraphicsSceneMouseMove (155), GraphicsSceneWheel (168)

**Touch & Gesture:** TouchBegin (194), TouchUpdate (195), TouchEnd (196), Gesture (198)

**Window Events:** WindowActivate (24), WindowDeactivate (25), WindowStateChange (105)

**Custom Events:** User (1000) through MaxUser (65535) for application-defined events

## 32 Event Subclasses

Direct inheritance includes: QActionEvent, QChildEvent, QCloseEvent, QDropEvent, QDragLeaveEvent, QFocusEvent, QGestureEvent, QHelpEvent, QInputEvent, QKeyEvent (via QInputEvent), QMouseEvent (via QInputEvent), QPaintEvent, QResizeEvent, QTimerEvent, QWheelEvent, and others.

## Properties

**accepted (bool):** Controls whether the event was handled. "Setting the accept parameter indicates that the event receiver wants the event."

## Design Pattern

Events can originate from the system (spontaneous = true) or be manually posted via QCoreApplication::sendEvent() and QCoreApplication::postEvent(). Objects receive events through their QObject::event() method, with specialized handlers like timerEvent() and mouseMoveEvent().
