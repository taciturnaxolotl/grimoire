#pragma once

#include <QObject>
#include <QString>
#include <QList>
#include <memory>
#include "rm_SceneItem.hpp"
#include "rm_Line.hpp"

class GrimoireInjector : public QObject {
    Q_OBJECT
public:
    explicit GrimoireInjector(QObject *parent = nullptr);

    Q_INVOKABLE int loadStrokes(const QString& path);
    Q_INVOKABLE bool setupVtable();
    Q_INVOKABLE int itemCount() const { return m_items.size(); }
    Q_INVOKABLE bool isReady() const { return m_vtableReady; }
    Q_INVOKABLE bool injectToClipboard();

public slots:
    void loadAndInject();

private:
    QList<std::shared_ptr<SceneItem>> m_items;
    bool m_vtableReady = false;
    QString m_watchPath = "/tmp/grimoire_strokes.json";
    qint64 m_lastModTime = 0;
};
