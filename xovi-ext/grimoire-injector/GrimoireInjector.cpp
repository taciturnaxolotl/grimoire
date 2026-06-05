#include "GrimoireInjector.hpp"
#include "rm_SceneLineItem.hpp"

#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QFile>
#include <QFileInfo>
#include <QMetaObject>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlApplicationEngine>
#include <QGuiApplication>
#include <QQuickWindow>
#include <QQmlEngine>
#include <QGuiApplication>
#include <QWindow>
#include <QTabletEvent>
#include <QPointingDevice>
#include <QInputDevice>
#include <QPointF>
#include <cstdio>
#include <cmath>
#include <algorithm>
#include <functional>
#include <pthread.h>
#include <sys/stat.h>
#include <unistd.h>

static GrimoireInjector *g_instance = nullptr;

/* Provided by entry.c hooks */
extern "C" {
    void *grimoire_getQmlEngine(void);
    void *grimoire_getSceneController(void);
    int grimoire_checkReload(void);
    void *grimoire_getFramebuffer(int *w, int *h, int *bpl, int *fmt);
}

static void *watchThreadFunc(void *) {
    fprintf(stderr, "[grimoire] Watch thread started\n");
    sleep(5);  // Wait for xochitl to fully initialize

    const char *path = "/tmp/grimoire_strokes.json";
    long long lastMod = 0;

    while (true) {
        /* Check for hot-reload signal */
        if (grimoire_checkReload()) {
            fprintf(stderr, "[grimoire] Hot-reload signal received! Re-scanning...\n");
            lastMod = 0;  // Force re-read of stroke file
        }

        struct stat st;
        if (stat(path, &st) == 0 && st.st_mtime != lastMod) {
            lastMod = st.st_mtime;
            fprintf(stderr, "[grimoire] File changed (mtime=%lld), triggering load\n",
                    (long long)lastMod);
            if (g_instance) {
                // Call back into main thread
                QMetaObject::invokeMethod(g_instance, "loadAndInject", Qt::QueuedConnection);
            }
        }
        sleep(1);
    }
    return nullptr;
}

GrimoireInjector::GrimoireInjector(QObject *parent)
    : QObject(parent)
{
    g_instance = this;
    pthread_t tid;
    pthread_create(&tid, nullptr, watchThreadFunc, nullptr);
    pthread_detach(tid);
    fprintf(stderr, "[grimoire] Constructor done, watch thread spawned\n");
}

void GrimoireInjector::loadAndInject() {
    QFileInfo fi(m_watchPath);
    if (!fi.exists()) return;

    qint64 modTime = fi.lastModified().toMSecsSinceEpoch();
    if (modTime == m_lastModTime) return;

    m_lastModTime = modTime;
    fprintf(stderr, "[grimoire] File changed, injecting via synthetic tablet events...\n");

    int count = loadStrokes(m_watchPath);
    if (count == 0) return;

    /* Find the focused window to post events to */
    QWindow *targetWin = nullptr;
    QObject *inputTarget = nullptr;
    auto *guiApp = qobject_cast<QGuiApplication*>(QCoreApplication::instance());
    if (guiApp) {
        targetWin = guiApp->focusWindow();
        inputTarget = guiApp->focusObject();
        if (inputTarget) {
            fprintf(stderr, "[grimoire] Focus object: %s @ %p\n",
                    inputTarget->metaObject()->className(), (void*)inputTarget);
        }
        
        if (!targetWin) {
            for (QWindow *win : guiApp->topLevelWindows()) {
                if (win->isVisible()) {
                    targetWin = win;
                    break;
                }
            }
        }
        
        /* Check QQuickWindow properties for contentItem/activeFocusItem */
        if (targetWin) {
            const QMetaObject *mo = targetWin->metaObject();
            for (int i = mo->propertyOffset(); i < mo->propertyCount(); i++) {
                QMetaProperty prop = mo->property(i);
                if (strcmp(prop.name(), "contentItem") == 0 || 
                    strcmp(prop.name(), "activeFocusItem") == 0) {
                    QVariant val = prop.read(targetWin);
                    QObject *obj = val.value<QObject*>();
                    if (obj) {
                        fprintf(stderr, "[grimoire]   %s: %s @ %p\n",
                                prop.name(), obj->metaObject()->className(), (void*)obj);
                        if (!inputTarget) inputTarget = obj;
                    }
                }
            }
        }
    }

    if (!inputTarget && targetWin) {
        inputTarget = targetWin;
    }
    
    if (!inputTarget) {
        fprintf(stderr, "[grimoire] No input target found\n");
        return;
    }

    fprintf(stderr, "[grimoire] Posting %d strokes as tablet events to %s @ %p\n",
            count, inputTarget->metaObject()->className(), (void*)inputTarget);

    /* Find the pen/stylus pointing device */
    const QPointingDevice *penDevice = nullptr;
    QList<const QInputDevice*> allDevices = QInputDevice::devices();
    for (const QInputDevice *dev : allDevices) {
        const auto *pDev = dynamic_cast<const QPointingDevice*>(dev);
        if (!pDev) continue;
        fprintf(stderr, "[grimoire]   device: %s type=%d pointer=%d\n",
                pDev->name().toUtf8().constData(),
                (int)pDev->type(), (int)pDev->pointerType());
        if (pDev->pointerType() == QPointingDevice::PointerType::Pen) {
            penDevice = pDev;
            break;
        }
    }
    if (!penDevice) {
        /* Fallback: use first pointing device */
        for (const QInputDevice *dev : allDevices) {
            const auto *pDev = dynamic_cast<const QPointingDevice*>(dev);
            if (pDev) {
                penDevice = pDev;
                fprintf(stderr, "[grimoire] No pen device, using fallback: %s\n",
                        pDev->name().toUtf8().constData());
                break;
            }
        }
    }
    if (!penDevice) {
        fprintf(stderr, "[grimoire] No pointing devices available\n");
        return;
    }

    /* Post each stroke as a sequence of tablet events: press → move... → release */
    /* Transform from .rm v6 coords (origin top-center) to screen pixels (origin top-left) */
    /* Screen: 1404x1872. RM v6: x[-702,701] y[0,1871] → screen x = rm_x + 702, y = rm_y */
    const float xOff = 702.0f;
    const float yOff = 0.0f;

    for (int s = 0; s < m_items.size(); s++) {
        auto *lineItem = reinterpret_cast<SceneLineItem*>(m_items[s].get());
        const Line &line = lineItem->line;

        if (line.points.isEmpty()) continue;

        /* Tablet press at first point */
        const auto &first = line.points.first();
        QPointF pos(first.x + xOff, first.y + yOff);

        QTabletEvent press(
            QEvent::TabletPress, penDevice,
            pos, pos,
            first.pressure / 255.0,
            0, 0, 0, 0, 0,
            Qt::NoModifier,
            Qt::LeftButton,
            Qt::LeftButton
        );
        QCoreApplication::sendEvent(inputTarget, &press);

        /* Tablet move for intermediate points */
        for (int i = 1; i < line.points.size() - 1; i++) {
            const auto &pt = line.points[i];
            QPointF ppos(pt.x + xOff, pt.y + yOff);

            QTabletEvent move(
                QEvent::TabletMove, penDevice,
                ppos, ppos,
                pt.pressure / 255.0,
                0, 0, 0, 0, 0,
                Qt::NoModifier,
                Qt::NoButton,
                Qt::LeftButton
            );
            QCoreApplication::sendEvent(inputTarget, &move);
        }

        /* Tablet release at last point */
        const auto &last = line.points.last();
        QPointF lpos(last.x + xOff, last.y + yOff);

        QTabletEvent release(
            QEvent::TabletRelease, penDevice,
            lpos, lpos,
            last.pressure / 255.0,
            0, 0, 0, 0, 0,
            Qt::NoModifier,
            Qt::LeftButton,
            Qt::NoButton
        );
        QCoreApplication::sendEvent(inputTarget, &release);
    }

    fprintf(stderr, "[grimoire] Posted all tablet events\n");
}

int GrimoireInjector::loadStrokes(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        fprintf(stderr, "[grimoire] Cannot open %s\n", path.toUtf8().constData());
        return 0;
    }

    QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
    file.close();
    QJsonArray jsonArray = doc.array();

    fprintf(stderr, "[grimoire] Loading %d strokes from %s\n",
           jsonArray.size(), path.toUtf8().constData());

    m_items.clear();

    for (int i = 0; i < jsonArray.size(); i++) {
        QJsonObject obj = jsonArray[i].toObject();
        QJsonArray pointsArray = obj["points"].toArray();
        QList<LinePoint> linePoints;

        float minX = 1e9f, minY = 1e9f, maxX = -1e9f, maxY = -1e9f;

        for (int j = 0; j < pointsArray.size(); j++) {
            QJsonArray ptArr = pointsArray[j].toArray();
            LinePoint pt;
            pt.x = static_cast<float>(ptArr[0].toDouble());
            pt.y = static_cast<float>(ptArr[1].toDouble());
            pt.speed = static_cast<unsigned short>(ptArr[2].toInt());
            pt.width = static_cast<unsigned short>(ptArr[3].toInt());
            pt.direction = static_cast<unsigned char>(ptArr[4].toInt());
            pt.pressure = static_cast<unsigned char>(ptArr[5].toInt());
            linePoints.append(pt);

            minX = std::min(minX, pt.x);
            minY = std::min(minY, pt.y);
            maxX = std::max(maxX, pt.x);
            maxY = std::max(maxY, pt.y);
        }

        QRectF bounds(minX, minY, maxX - minX, maxY - minY);

        Line line;
        line.points = linePoints;
        line.bounds = bounds;
        line.rgba = static_cast<unsigned int>(obj["rgba"].toDouble(0xFF000000));
        line.color = obj["color"].toInt(0);
        line.tool = obj["tool"].toInt(0x0F); // BALLPOINT_2
        line.maskScale = obj["maskScale"].toDouble(1.0);
        line.thickness = static_cast<float>(obj["thickness"].toDouble(2.0));

        auto item = std::make_shared<SceneLineItem>(
            SceneLineItem::fromLine(std::move(line)));
        m_items.push_back(item);
    }

    fprintf(stderr, "[grimoire] Loaded %d scene items\n", m_items.size());
    return m_items.size();
}

bool GrimoireInjector::setupVtable() {
    if (m_items.empty()) {
        fprintf(stderr, "[grimoire] setupVtable: no items loaded\n");
        return false;
    }
    auto* item = reinterpret_cast<SceneLineItem*>(m_items.first().get());
    SceneLineItem::setupVtable(item->vtable);
    m_vtableReady = true;
    fprintf(stderr, "[grimoire] Vtable ready\n");
    return true;
}
