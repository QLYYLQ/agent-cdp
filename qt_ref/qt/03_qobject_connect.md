# QObject::connect() and QObject::disconnect() Technical Reference

> Source: https://doc.qt.io/qt-6/qobject.html#connect

## QObject::connect() Overloads

### 1. Meta-Method Based Connection
```cpp
static QMetaObject::Connection connect(
    const QObject *sender,
    const QMetaMethod &signal,
    const QObject *receiver,
    const QMetaMethod &method,
    Qt::ConnectionType type = Qt::AutoConnection)
```

Creates a connection using `QMetaMethod` objects instead of string signatures. This approach enables runtime signal/slot verification through the meta-object system.

### 2. String-Based Connection (Traditional)
```cpp
static QMetaObject::Connection connect(
    const QObject *sender,
    const char *signal,
    const QObject *receiver,
    const char *method,
    Qt::ConnectionType type = Qt::AutoConnection)
```

**Requirements:** Signal and method parameters must use `SIGNAL()` and `SLOT()` macros without variable names—only type information.

**Return value:** `QMetaObject::Connection` handle for later disconnection; invalid handle if connection fails.

**Duplicate connections:** "By default, a signal is emitted for every connection you make; two signals are emitted for duplicate connections."

**Unique connection type:** Using `Qt::UniqueConnection` prevents duplicate connections but applies only to member function connections, not lambdas or functors.

**Signal-to-signal connections:** Signals can be connected to other signals for relaying:
```cpp
class MyWidget : public QWidget {
    Q_OBJECT
public:
    MyWidget();
signals:
    void buttonClicked();
private:
    QPushButton *myButton;
};

MyWidget::MyWidget() {
    myButton = new QPushButton(this);
    connect(myButton, SIGNAL(clicked()),
            this, SIGNAL(buttonClicked()));
}
```

### 3. Context-Less Functor Connection
```cpp
static template <typename PointerToMemberFunction, typename Functor>
QMetaObject::Connection connect(
    const QObject *sender,
    PointerToMemberFunction signal,
    Functor functor)
```

**Automatic disconnection:** "The connection will automatically disconnect if the sender is destroyed."

**Caution:** Objects used within the functor must remain alive when the signal is emitted.

### 4. Functor with Context Object
```cpp
static template <typename PointerToMemberFunction, typename Functor>
QMetaObject::Connection connect(
    const QObject *sender,
    PointerToMemberFunction signal,
    const QObject *context,
    Functor functor,
    Qt::ConnectionType type = Qt::AutoConnection)
```

Recommended approach for lambda connections; provides explicit event loop context.

**Automatic disconnection:** Triggered when either sender or context object is destroyed.

### 5. Pointer-to-Member-Function Connection
```cpp
static template <typename PointerToMemberFunction>
QMetaObject::Connection connect(
    const QObject *sender,
    PointerToMemberFunction signal,
    const QObject *receiver,
    PointerToMemberFunction method,
    Qt::ConnectionType type = Qt::AutoConnection)
```

**Advantages:** Compile-time verification of signal/slot existence and signature compatibility.

**Unique connections:** "Qt::UniqueConnections do not work for lambdas, non-member functions and functors; they only apply to connecting to member functions."

---

## QObject::disconnect() Overloads

### 1. Connection Handle Disconnection
```cpp
static bool disconnect(const QMetaObject::Connection &connection)
```

Disconnects using the handle returned by `connect()`. Returns false if connection is invalid or already disconnected.

### 2. String-Based Disconnection
```cpp
static bool disconnect(
    const QObject *sender,
    const char *signal,
    const QObject *receiver,
    const char *method)
```

**Common usage patterns:**

1. Disconnect everything from an object's signals: `disconnect(myObject, nullptr, nullptr, nullptr);`
2. Disconnect everything from a specific signal: `disconnect(myObject, SIGNAL(mySignal()), nullptr, nullptr);`
3. Disconnect a specific receiver: `disconnect(myObject, nullptr, myReceiver, nullptr);`

**Wildcard rules:** `nullptr` may be used as a wildcard, meaning 'any signal', 'any receiving object', or 'any slot in the receiving object'.

**Constraint:** "The _sender_ may never be `nullptr`."

### 3. Member Function Based Disconnection
```cpp
static template <typename PointerToMemberFunction>
bool disconnect(
    const QObject *sender,
    PointerToMemberFunction signal,
    const QObject *receiver,
    PointerToMemberFunction method)
```

**Limitation:** Cannot disconnect functors or lambda expressions. Use `QMetaObject::Connection` handle instead.

## Automatic Disconnection on Object Destruction

"A signal-slot connection is removed when either of the objects involved are destroyed."

When the sender is destroyed, all its signal connections are automatically severed. When the receiver is destroyed, all signals connected to its slots are severed.

## Queued Connection Disconnection Caveat

"If a queued connection is disconnected, already scheduled events may still be delivered, causing the receiver to be called after the connection is disconnected."

## Thread Safety

"Note: This function is thread-safe." — applies to all static connect()/disconnect() variants.
