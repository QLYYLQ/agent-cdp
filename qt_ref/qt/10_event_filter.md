# Event Filtering in QObject

> Source: https://doc.qt.io/qt-6/qobject.html#installEventFilter

## installEventFilter()

**Signature:**
```cpp
void QObject::installEventFilter(QObject *filterObj)
```

"Installs an event filter filterObj on this object." The filter object receives all events sent to the monitored object through its `eventFilter()` function.

**Filter Return Values:**
The `eventFilter()` must return `true` if "the event should be filtered, (i.e. stopped); otherwise it must return false" to allow standard event processing.

**Multiple Filters - Ordering:**
"If multiple event filters are installed on a single object, the filter that was installed last is activated first."

**Reinstallation:**
"If filterObj has already been installed for this object, this function moves it so it acts as if it was installed last."

**Thread Requirements:**
"Note that the filtering object must be in the same thread as this object. If filterObj is in a different thread, this function does nothing."

**Thread Affinity After Installation:**
"If either filterObj or this object are moved to a different thread after calling this function, the event filter will not be called until both objects have the same thread affinity again (it is not removed)."

**Safety Warning:**
"If you delete the receiver object in your eventFilter() function, be sure to return true. If you return false, Qt sends the event to the deleted object and the program will crash."

## removeEventFilter()

Counterpart to `installEventFilter()`. Removes a previously installed event filter object.

## eventFilter()

**Signature:**
```cpp
virtual bool QObject::eventFilter(QObject *watched, QEvent *event)
```

"Filters events if this object has been installed as an event filter for the watched object."

**Return Semantics:**
"If you want to filter the event out, i.e. stop it being handled further, return true; otherwise return false."

**Unhandled Events:**
"Unhandled events are passed to the base class's eventFilter() function, since the base class might have reimplemented eventFilter() for its own internal purposes."

**Special Events:**
"Some events, such as QEvent::ShortcutOverride must be explicitly accepted (by calling accept() on them) in order to prevent propagation."

**Safety Warning:**
"If you delete the receiver object in this function, be sure to return true. Otherwise, Qt will forward the event to the deleted object and the program might crash."

## event()

**Signature:**
```cpp
virtual bool QObject::event(QEvent *e)
```

"Receives events to an object and should return true if the event e was recognized and processed."

**Implementation Requirement:**
"Make sure you call the parent event class implementation for all the events you did not handle."

## Event Processing Order

1. Event filters installed on the object (last installed → first activated)
2. Global filters on QApplication/QCoreApplication
3. QObject::event() virtual function
4. Event-specific handlers (mousePressEvent, keyPressEvent, etc.)
5. Parent propagation (for certain event types like key/mouse events)
