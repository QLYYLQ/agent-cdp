# The Event System - Qt 6.10.2

> Source: https://doc.qt.io/qt-6/eventsandfilters.html

## Overview

In Qt, events are objects derived from the abstract `QEvent` class representing occurrences within an application or from external activity that requires handling. Any `QObject` subclass instance can receive and handle events, though they're particularly relevant to widgets.

## How Events are Delivered

When an event occurs, Qt constructs an instance of the appropriate `QEvent` subclass and delivers it to a `QObject` instance by calling its `event()` function.

The `event()` function doesn't handle the event itself. Instead, based on event type, it calls an event-specific handler and sends a response indicating whether the event was accepted or ignored.

Event sources include:
- Window system events: `QMouseEvent`, `QKeyEvent`
- Internal sources: `QTimerEvent`
- Application-generated events

## Event Types

Most event types have specialized classes:
- `QResizeEvent` — adds `size()` and `oldSize()` functions
- `QPaintEvent` — painting operations
- `QMouseEvent` — button presses, double-clicks, moves
- `QKeyEvent` — keyboard input
- `QCloseEvent` — window closing

Each event has an associated type defined in `QEvent::Type` for runtime type identification.

## Event Handlers

The standard delivery method uses virtual functions. For example, `QPaintEvent` is delivered via `QWidget::paintEvent()`, which reacts appropriately.

### Example: Custom Checkbox

```cpp
void MyCheckBox::mousePressEvent(QMouseEvent *event)
{
    if (event->button() == Qt::LeftButton) {
        // handle left mouse button here
    } else {
        // pass on other buttons to base class
        QCheckBox::mousePressEvent(event);
    }
}
```

When extending base functionality, implement custom behavior and call the base class for default handling.

### Reimplementing QObject::event()

For situations without event-specific functions or when those functions are insufficient (such as Tab key handling), reimplement `QObject::event()`:

```cpp
bool MyWidget::event(QEvent *event)
{
    if (event->type() == QEvent::KeyPress) {
        QKeyEvent *ke = static_cast<QKeyEvent *>(event);
        if (ke->key() == Qt::Key_Tab) {
            // special tab handling here
            return true;
        }
    } else if (event->type() == MyCustomEventType) {
        MyCustomEvent *myEvent = static_cast<MyCustomEvent *>(event);
        // custom event handling here
        return true;
    }

    return QWidget::event(event);
}
```

The return value indicates whether the event was handled. Returning `true` prevents further event propagation.

## Event Filters

Event filters allow one object to intercept events intended for another. Install filters using `QObject::installEventFilter()`, causing the filter object to receive target object events in its `QObject::eventFilter()` function.

Event filters process events before the target object, allowing inspection and discarding. Remove filters with `QObject::removeEventFilter()`.

### Filter Processing

- If all event filters return `false`, the event reaches the target object
- If a filter returns `true`, subsequent filters and the target don't see the event

### Example: Tab Key Filtering

```cpp
bool FilterObject::eventFilter(QObject *object, QEvent *event)
{
    if (object == target && event->type() == QEvent::KeyPress) {
        QKeyEvent *keyEvent = static_cast<QKeyEvent *>(event);
        if (keyEvent->key() == Qt::Key_Tab) {
            // Special tab handling
            return true;
        } else
            return false;
    }
    return false;
}
```

### Global Filters

Installing event filters on `QApplication` or `QCoreApplication` creates global filters processed before object-specific filters. This is powerful but slows all event delivery; specific techniques should generally be preferred.

## Sending Events

Applications can create and send custom events using `QCoreApplication::sendEvent()` and `QCoreApplication::postEvent()`.

### sendEvent()

Processes events immediately. When it returns, event filters and/or the object have already processed the event. Use `isAccepted()` to determine if the event was accepted or rejected.

### postEvent()

Posts events on a queue for later dispatch during the next main event loop cycle. Includes optimizations: multiple resize or paint events are compressed. `QWidget::update()` calls `postEvent()`, eliminating flickering and improving performance.

**Initialization note:** Events are typically dispatched soon after object initialization. Initialize member variables early in constructors before events can arrive.

### Custom Events

Create custom event types by:
1. Defining an event number greater than `QEvent::User`
2. Subclassing `QEvent` to pass custom information
