# QCoreApplication Event Handling Methods

> Source: https://doc.qt.io/qt-6/qcoreapplication.html#sendEvent

## sendEvent()

**Signature:**
```cpp
static bool sendEvent(QObject *receiver, QEvent *event)
```

"Sends event directly to receiver, using the notify() function. Returns the value that was returned from the event handler."

The event is not deleted after dispatch. The typical pattern involves stack-allocated events:

```cpp
QMouseEvent event(QEvent::MouseButtonPress, pos, 0, 0, 0);
QApplication::sendEvent(mainWindow, &event);
```

**Key characteristics:**
- Synchronous delivery
- Event remains valid after the call
- Return value reflects the handler's response

---

## postEvent()

**Signature:**
```cpp
static void postEvent(QObject *receiver, QEvent *event,
                     int priority = Qt::NormalEventPriority)
```

"Adds the event to an event queue and returns immediately."

**Important requirements:**
- "The event must be allocated on the heap since the post event queue will take ownership of the event and delete it once it has been posted."
- "It is _not safe_ to access the event after it has been posted."

**Priority handling:**
"Events are sorted in descending priority order, i.e. events with a high priority are queued before events with a lower priority. The priority can be any integer value, i.e. between INT_MAX and INT_MIN, inclusive."

**Thread safety:**
"This function is thread-safe."

---

## processEvents()

**Overload 1 - Basic:**
```cpp
static void processEvents(QEventLoop::ProcessEventsFlags flags =
                         QEventLoop::AllEvents)
```

"Processes some pending events for the calling thread according to the specified flags."

**Overload 2 - With deadline (Qt 6.7+):**
```cpp
static void processEvents(QEventLoop::ProcessEventsFlags flags,
                         QDeadlineTimer deadline)
```

"Processes pending events for the calling thread until deadline has expired, or until there are no more events to process, whichever happens first."

Notable difference: "Unlike the processEvents() overload, this function also processes events that are posted while the function runs."

**Overload 3 - With timeout:**
```cpp
static void processEvents(QEventLoop::ProcessEventsFlags flags,
                         int ms)
```

**General notes:**
- "Use of this function is discouraged. Instead, prefer to move long operations out of the GUI thread into an auxiliary one."
- "This function is thread-safe."

---

## notify()

**Signature:**
```cpp
virtual bool notify(QObject *receiver, QEvent *event)
```

"Sends event to receiver: receiver->event(event). Returns the value that is returned from the receiver's event handler."

**Scope:**
"Note that this function is called for all events sent to any object in any thread."

**Event propagation:**
"For certain types of events (e.g. mouse and key events), the event will be propagated to the receiver's parent and so on up to the top-level object if the receiver is not interested in the event (i.e., it returns false)."

**Five event processing approaches listed (in order of generality):**
1. Reimplementing specific event methods (paintEvent, mousePressEvent, etc.)
2. Reimplementing `notify()` (most powerful, single subclass)
3. Installing application-level event filter
4. Reimplementing `QObject::event()`
5. Installing object-specific event filter

**Future compatibility warning:**
"This function will not be called for objects that live outside the main thread in Qt 7."

---

## sendPostedEvents()

**Signature:**
```cpp
static void sendPostedEvents(QObject *receiver = nullptr,
                            int event_type = 0)
```

"Immediately dispatches all events which have been previously queued with postEvent() and which are for the object receiver and have the event type event_type."

**Thread requirement:**
"This method must be called from the thread in which its QObject parameter, receiver, lives."

**Parameter behavior:**
- Null receiver: sends all events of the specified type
- Zero event type: sends all events for the receiver

---

## removePostedEvents()

**Signature:**
```cpp
static void removePostedEvents(QObject *receiver, int eventType = 0)
```

Removes queued events without dispatching them. Documentation cautions that "killing events may cause receiver to break one or more invariants."

"This function is thread-safe."
