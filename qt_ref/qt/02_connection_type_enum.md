# Qt::ConnectionType Enum

> Source: https://doc.qt.io/qt-6/qt.html#ConnectionType-enum

## Definition
The ConnectionType enum describes the types of connection available between signals and slots, determining whether a signal is delivered to a slot immediately or queued for later delivery.

## Enum Values

| Constant | Value | Description |
|----------|-------|-------------|
| `Qt::AutoConnection` | `0` | **(Default)** If the receiver resides in the thread that emits the signal, Qt::DirectConnection is used. Otherwise, Qt::QueuedConnection is used. The connection type is determined when the signal is emitted. |
| `Qt::DirectConnection` | `1` | The slot is invoked immediately when the signal is emitted. The slot executes in the signalling thread. |
| `Qt::QueuedConnection` | `2` | The slot is invoked when control returns to the event loop of the receiver's thread. The slot executes in the receiver's thread. |
| `Qt::BlockingQueuedConnection` | `3` | Same as Qt::QueuedConnection, except the signalling thread blocks until the slot returns. This connection must _not_ be used if the receiver lives in the signalling thread, or else the application will deadlock. |
| `Qt::UniqueConnection` | `0x80` | A flag that can be combined with any one of the above connection types, using a bitwise OR. When set, QObject::connect() will fail if the connection already exists. |
| `Qt::SingleShotConnection` | `0x100` | A flag that can be combined with any one of the above connection types, using a bitwise OR. When set, the slot is called only once; the connection breaks automatically when the signal is emitted. (Introduced in Qt 6.0) |

## Important Notes

**Queued connections require registered meta-types.** When using queued connections, the parameters must be of types known to Qt's meta-object system, since Qt needs to copy arguments to store them in an event. If you attempt to use a queued connection with unregistered types, you'll receive an error: "QObject::connect: Cannot queue arguments of type 'MyType'". Use qRegisterMetaType() to register the data type before establishing the connection.
