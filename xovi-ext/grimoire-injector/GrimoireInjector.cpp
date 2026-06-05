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
#include <cstdio>
#include <cmath>
#include <algorithm>
#include <pthread.h>
#include <sys/stat.h>
#include <unistd.h>

static GrimoireInjector *g_instance = nullptr;

static void *watchThreadFunc(void *) {
    fprintf(stderr, "[grimoire] Watch thread started\n");
    sleep(5);  // Wait for xochitl to fully initialize

    const char *path = "/tmp/grimoire_strokes.json";
    long long lastMod = 0;

    while (true) {
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
    // Spawn a real pthread for file watching — no Qt event loop dependency
    pthread_t tid;
    pthread_create(&tid, nullptr, watchThreadFunc, nullptr);
    pthread_detach(tid);
    fprintf(stderr, "[grimoire] Constructor done, watch thread spawned\n");
}

void GrimoireInjector::loadAndInject() {
    fprintf(stderr, "[grimoire] loadAndInject called on main thread\n");
    int count = loadStrokes(m_watchPath);
    if (count > 0) {
        if (!m_vtableReady) setupVtable();
        injectToClipboard();
    }
}

bool GrimoireInjector::injectToClipboard() {
    if (m_items.empty()) {
        fprintf(stderr, "[grimoire] injectToClipboard: no items\n");
        return false;
    }

    // Find the QQmlEngine via the application's root objects
    QQmlEngine *engine = nullptr;
    auto *app = qobject_cast<QGuiApplication*>(QCoreApplication::instance());
    if (!app) {
        fprintf(stderr, "[grimoire] No QGuiApplication\n");
        return false;
    }

    // Try to find engine from top-level windows' QML engines
    // The clipboard model is typically a context property on the root context
    // We'll try to access it via the first available engine
    QList<QQmlEngine*> engines;

    // Walk through all QObjects to find QQmlEngine
    // Simpler: use QQmlEngine::contextForObject on any known QML object
    // For now, try to find it via the global app properties

    // Alternative approach: directly manipulate the clipboard via
    // xochitl's internal Clipboard model. The model is registered as
    // a context property "Clipboard" in the root QML context.
    // We need to find the engine first.

    // Try iterating over top-level objects to find a QML-created object
    for (QObject *obj : app->children()) {
        QQmlContext *ctx = QQmlEngine::contextForObject(obj);
        if (ctx) {
            engine = ctx->engine();
            break;
        }
    }

    if (!engine) {
        // Fallback: try to get engine from any QQuickWindow
        fprintf(stderr, "[grimoire] Could not find QQmlEngine from app children\n");
        // Try another approach - enumerate all objects
        for (QObject *obj : QObjectList()) {
            QQmlContext *ctx = QQmlEngine::contextForObject(obj);
            if (ctx) {
                engine = ctx->engine();
                break;
            }
        }
    }

    if (!engine) {
        fprintf(stderr, "[grimoire] No QQmlEngine found, cannot inject\n");
        return false;
    }

    fprintf(stderr, "[grimoire] Found QQmlEngine at %p\n", (void*)engine);

    // Get root context and look for Clipboard
    QQmlContext *rootCtx = engine->rootContext();
    if (!rootCtx) {
        fprintf(stderr, "[grimoire] No root context\n");
        return false;
    }

    QObject *clipboard = rootCtx->contextProperty("Clipboard").value<QObject*>();
    if (!clipboard) {
        fprintf(stderr, "[grimoire] No Clipboard context property found\n");
        // List available context properties for debugging
        fprintf(stderr, "[grimoire] Trying to find clipboard-like objects...\n");
        return false;
    }

    fprintf(stderr, "[grimoire] Found Clipboard object at %p\n", (void*)clipboard);

    // Set items on the clipboard model
    // The clipboard model has an 'items' property that accepts QList<shared_ptr<SceneItem>>
    QVariant itemsVariant = QVariant::fromValue(m_items);
    bool ok = clipboard->setProperty("items", itemsVariant);
    fprintf(stderr, "[grimoire] Set Clipboard.items: %s (%d items)\n",
            ok ? "ok" : "FAILED", m_items.size());

    return ok;
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
