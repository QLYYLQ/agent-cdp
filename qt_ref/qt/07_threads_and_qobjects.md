# Threads and QObjects - Qt 6.10

> Source: https://doc.qt.io/qt-6/threads-qobject.html

## QObject Reentrancy

QThread inherits QObject and emits signals indicating thread execution start/finish. QObjects can be used across multiple threads, emit signals invoking slots in other threads, and post events to objects in different threads. Each thread maintains its own event loop.

QObject is reentrant, as are most non-GUI subclasses like QTimer, QTcpSocket, QUdpSocket, and QProcess. However, three constraints apply:

1. **Parent-Child Thread Affinity**: "The child of a QObject must always be created in the thread where the parent was created." Never pass QThread (`this`) as a parent to objects created in that thread.

2. **Event-Driven Objects**: "Event driven objects may only be used in a single thread." This restricts timer mechanisms and network module usage. You cannot start a timer or connect a socket outside the object's thread.

3. **Object Cleanup**: "You must ensure that all objects created in a thread are deleted before you delete the QThread." Create objects on the stack in `run()` implementation for easy management.

**GUI Limitation**: GUI classes like QWidget and subclasses are not reentrant and can only be used from the main thread. QCoreApplication::exec() must also be called from the main thread.

## Per-Thread Event Loop

Each thread can maintain its own event loop. The initial thread starts via QCoreApplication::exec() or QDialog::exec(). Other threads use QThread::exec(). Both QCoreApplication and QThread provide exit(int) functions and quit() slots.

Event loops enable threads to use non-GUI Qt classes requiring event loops (QTimer, QTcpSocket, QProcess) and allow connecting signals from any thread to specific thread slots.

**Thread Affinity**: A QObject "lives" in the thread where it was created. Events dispatch through that thread's event loop. Retrieve thread affinity using QObject::thread().

**Moving Objects**: QObject::moveToThread() changes thread affinity for an object and its children (objects with parents cannot be moved).

**Safe Deletion**: "Calling `delete` on a QObject from a thread other than the one that owns the object...is unsafe." Use QObject::deleteLater() instead, which posts a DeferredDelete event for the object's event loop to handle.

**Event Delivery Requirements**: If no event loop runs, events won't deliver. Creating a QTimer without calling exec() means the timeout() signal never emits, and deleteLater() won't work either.

**Manual Event Posting**: Use the thread-safe QCoreApplication::postEvent() to manually post events to any object in any thread at any time.

**Event Filter Restrictions**: Event filters work in all threads with the restriction that monitoring objects must live in the same thread as monitored objects. QCoreApplication::sendEvent() can only dispatch events to objects in the calling thread.

## Accessing QObject Subclasses from Other Threads

"QObject and all of its subclasses are not thread-safe. This includes the entire event delivery system." The event loop may deliver events while you access the object from another thread.

"If you are calling a function on an QObject subclass that doesn't live in the current thread...you must protect all access...with a mutex."

QThread objects live in their creation thread, not the thread created by QThread::run(). Providing slots in QThread subclasses is generally unsafe without mutex protection on member variables.

**Safe Signal Emission**: You can safely emit signals from QThread::run() implementation, as signal emission is thread-safe.

## Signals and Slots Across Threads

Qt supports five signal-slot connection types:

1. **Auto Connection (default)**: "If the signal is emitted in the thread which the receiving object has affinity then the behavior is the same as the Direct Connection. Otherwise, the behavior is the same as the Queued Connection."

2. **Direct Connection**: Slot invokes immediately when signal emits. Slot executes in emitter's thread, not necessarily receiver's thread.

3. **Queued Connection**: Slot invokes when control returns to receiver's thread event loop. Slot executes in receiver's thread.

4. **Blocking Queued Connection**: Slot invokes as Queued Connection, but current thread blocks until slot returns. **Warning**: "Using this type to connect objects in the same thread will cause deadlock."

5. **Unique Connection**: Behaves like Auto Connection but only establishes connection if it doesn't duplicate an existing connection.

Specify connection type via additional argument to connect(). Direct connections across threads with running event loops are unsafe, as calling functions on objects in other threads is unsafe.

"QObject::connect() itself is thread-safe."
