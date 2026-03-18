# The Property System - Qt Core 6.10.2

> Source: https://doc.qt.io/qt-6/properties.html

## Overview

Qt provides a compiler- and platform-independent property system based on the Meta-Object System that enables inter-object communication via signals and slots.

## Requirements for Declaring Properties

To declare a property, use the `Q_PROPERTY()` macro in a class inheriting `QObject`:

```cpp
Q_PROPERTY(type name
           (READ getFunction [WRITE setFunction] |
            MEMBER memberName [(READ getFunction | WRITE setFunction)])
           [RESET resetFunction]
           [NOTIFY notifySignal]
           [REVISION int | REVISION(int[, int])]
           [DESIGNABLE bool]
           [SCRIPTABLE bool]
           [STORED bool]
           [USER bool]
           [BINDABLE bindableProperty]
           [CONSTANT]
           [FINAL]
           [REQUIRED])
```

### Typical Examples

```cpp
Q_PROPERTY(bool focus READ hasFocus)
Q_PROPERTY(bool enabled READ isEnabled WRITE setEnabled)
Q_PROPERTY(QCursor cursor READ cursor WRITE setCursor RESET unsetCursor)
```

### Using MEMBER Keyword

```cpp
Q_PROPERTY(QColor color MEMBER m_color NOTIFY colorChanged)
Q_PROPERTY(qreal spacing MEMBER m_spacing NOTIFY spacingChanged)
Q_PROPERTY(QString text MEMBER m_text NOTIFY textChanged)
...
signals:
    void colorChanged();
    void spacingChanged();
    void textChanged(const QString &newText);

private:
    QColor  m_color;
    qreal   m_spacing;
    QString m_text;
```

## Property Attributes

**READ Function**
- Required if no `MEMBER` variable specified
- Should be const and return property type or const reference

**WRITE Function**
- Optional for setting property value
- Must return void and accept exactly one parameter

**MEMBER Variable**
- Required if no `READ` accessor specified
- Makes member readable/writable without accessor functions

**RESET Function**
- Optional for restoring property to context-specific default
- Must return void and take no parameters

**NOTIFY Signal**
- Optional signal emitted when property value changes
- For `MEMBER` variables: must take zero or one parameter of same type
- Only emit when property actually changes to avoid unnecessary re-evaluation
- Emitted automatically via Qt API (QObject::setProperty, QMetaProperty)

**BINDABLE**
- Supports bindings and binding inspection via meta object system
- Names class member of type `QBindable<T>` where T is property type
- Introduced in Qt 6.0

**CONSTANT**
- Indicates property value is constant
- Cannot have `WRITE` method or `NOTIFY` signal

**FINAL**
- Indicates property will not be overridden by derived class
- For performance optimizations (not enforced by moc)

**REQUIRED**
- Indicates property should be set by class user

## Reading and Writing Properties with Meta-Object System

### Generic Access

```cpp
QPushButton *button = new QPushButton;
QObject *object = button;

button->setDown(true);
object->setProperty("down", true);
```

### Property Discovery

```cpp
QObject *object = ...
const QMetaObject *metaobject = object->metaObject();
int count = metaobject->propertyCount();
for (int i=0; i<count; ++i) {
    QMetaProperty metaproperty = metaobject->property(i);
    const char *name = metaproperty.name();
    QVariant value = object->property(name);
    ...
}
```

## A Simple Example

```cpp
class MyClass : public QObject
{
    Q_OBJECT
    Q_PROPERTY(Priority priority READ priority WRITE setPriority NOTIFY priorityChanged)

public:
    MyClass(QObject *parent = nullptr);
    ~MyClass();

    enum Priority { High, Low, VeryHigh, VeryLow };
    Q_ENUM(Priority)

    void setPriority(Priority priority)
    {
        if (m_priority == priority)
            return;
        m_priority = priority;
        emit priorityChanged(priority);
    }
    Priority priority() const
    { return m_priority; }

signals:
    void priorityChanged(Priority);

private:
    Priority m_priority;
};
```

## Dynamic Properties

`QObject::setProperty()` can add new properties at runtime. Dynamic properties are per-instance.

Remove property by passing invalid `QVariant`:
```cpp
object->setProperty("propertyName", QVariant());
```

## Using Bindable Properties

Three types implement bindable properties:
- `QProperty` - general class
- `QObjectBindableProperty` - for use inside QObject
- `QObjectComputedProperty` - for use inside QObject
