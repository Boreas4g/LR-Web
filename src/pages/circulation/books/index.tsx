import {
  CheckCircleOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  PictureOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  createBook,
  DEFAULT_BOOK_HEIGHT_MM,
  deleteBook,
  exportLibraryBackup,
  getMapAnnotations,
  getMapVersions,
  importBooks,
  listBooks,
  updateBook,
} from '@/services/libraryRobot';
import {
  importLibraryImage,
  type LibraryItem,
  libraryImageUrl,
  listLibrary,
  verifyLibrary,
} from '@/services/libraryRobot/studio';
import type {
  Book,
  BookImportResult,
  MapAnnotation,
} from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';
import AnnotationTargetSelect from '../components/AnnotationTargetSelect';
import {
  filterBooksByKeyword,
  findDuplicateBookName,
  formatBookTime,
  missingTemplateIds,
  pagedBooks,
} from './bookUtils';
import { FALLBACK_SPINE_ASPECT, medianSpineAspect } from './cropUtils';
import SpineCropModal from './SpineCropModal';

// 与 bookarm 书脊库接受的格式对齐（不收 WebP）。
const SPINE_IMAGE_TYPES = ['image/jpeg', 'image/png'];
const SPINE_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
const HEIGHT_TOOLTIP = '该书所在层的升降台目标高度，非书本物理高度';
/** 机器人书立槽位（bookarm 标定）。 */
const BOOK_STAND_SLOTS = [1, 2, 3, 4, 5];

function revokeUrls(urls: Record<string, string>) {
  for (const url of Object.values(urls)) URL.revokeObjectURL(url);
}

type CheckRow = {
  book: Book;
  status: 'ok' | '特征不足' | '模板缺失';
  detail?: string;
};

/** Book management: name + spine image (segmented/library/local) + lift height
 * + takeout point & preferred slot association. */
export default function BooksPage() {
  const { message } = App.useApp();
  const [books, setBooks] = useState<Book[]>([]);
  const [annotations, setAnnotations] = useState<MapAnnotation[]>([]);
  // 当前页书脊图缓存：null 表示拉取失败（模板缺失或网络错误）。
  const [spineUrls, setSpineUrls] = useState<Record<string, string | null>>({});
  const [libraryIds, setLibraryIds] = useState<Set<string>>();
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<Book>();
  const [name, setName] = useState('');
  const [templateId, setTemplateId] = useState<string>();
  const [spineFile, setSpineFile] = useState<File>();
  const [spineFileUrl, setSpineFileUrl] = useState<string>();
  const [cropFile, setCropFile] = useState<File>();
  const [referenceAspect, setReferenceAspect] = useState(FALLBACK_SPINE_ASPECT);
  const [heightMm, setHeightMm] = useState(DEFAULT_BOOK_HEIGHT_MM);
  const [annotationId, setAnnotationId] = useState<string>();
  const [preferredSlot, setPreferredSlot] = useState<number>();
  const [saving, setSaving] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [csvFile, setCsvFile] = useState<File>();
  const [zipFile, setZipFile] = useState<File>();
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<BookImportResult>();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerLoading, setPickerLoading] = useState(false);
  const [libraryItems, setLibraryItems] = useState<LibraryItem[]>([]);
  const [pickerUrls, setPickerUrls] = useState<Record<string, string>>({});
  const [checking, setChecking] = useState(false);
  const [checkOpen, setCheckOpen] = useState(false);
  const [checkRows, setCheckRows] = useState<CheckRow[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [bookList, library] = await Promise.all([
        listBooks(),
        // 书脊库清单只取 id 用于「模板缺失」检测；服务不可达时跳过标红。
        listLibrary().catch(() => null),
      ]);
      setBooks(bookList);
      setLibraryIds(
        library ? new Set(library.items.map((item) => item.id)) : undefined,
      );
      setSpineUrls((previous) => {
        for (const url of Object.values(previous)) {
          if (url) URL.revokeObjectURL(url);
        }
        return {};
      });
    } catch (error) {
      message.error(errorMessage(error, '无法加载书籍列表。'));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void refresh();
    (async () => {
      try {
        const maps = await getMapVersions();
        const groups = await Promise.all(
          maps.map((item) => getMapAnnotations(item.id).catch(() => [])),
        );
        setAnnotations(groups.flat());
      } catch {
        // 点位下拉留空：书籍仍可登记，只是暂不能关联取书点。
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(
    () => filterBooksByKeyword(books, keyword),
    [books, keyword],
  );
  const pageItems = useMemo(
    () => pagedBooks(filtered, page, pageSize),
    [filtered, page, pageSize],
  );
  const missingTemplates = useMemo(
    () =>
      libraryIds ? missingTemplateIds(books, libraryIds) : new Set<string>(),
    [books, libraryIds],
  );
  const duplicate = useMemo(
    () => findDuplicateBookName(books, name, editing?.id),
    [books, name, editing],
  );

  // 书脊图懒加载：只拉当前页出现、且尚未缓存的模板。
  useEffect(() => {
    const ids = [...new Set(pageItems.map((book) => book.templateId))].filter(
      (id) => !(id in spineUrls),
    );
    if (!ids.length) return;
    let cancelled = false;
    (async () => {
      const entries = await Promise.all(
        ids.map(
          async (id) =>
            [id, await libraryImageUrl(id).catch(() => null)] as const,
        ),
      );
      if (cancelled) {
        for (const [, url] of entries) {
          if (url) URL.revokeObjectURL(url);
        }
        return;
      }
      setSpineUrls((previous) => ({
        ...previous,
        ...Object.fromEntries(entries),
      }));
    })();
    return () => {
      cancelled = true;
    };
  }, [pageItems, spineUrls]);

  useEffect(
    () => () => {
      for (const url of Object.values(spineUrls)) {
        if (url) URL.revokeObjectURL(url);
      }
      revokeUrls(pickerUrls);
      if (spineFileUrl) URL.revokeObjectURL(spineFileUrl);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  function setSpineUpload(file: File | undefined) {
    setSpineFile(file);
    setSpineFileUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return file ? URL.createObjectURL(file) : undefined;
    });
  }

  // 参考裁剪比例：解码当前页已缓存的书脊图（最多 8 张），取 w/h 中位数。
  async function measureReferenceAspect(): Promise<number> {
    const urls = Object.values(spineUrls)
      .filter((url): url is string => Boolean(url))
      .slice(0, 8);
    const dims = await Promise.all(
      urls.map(async (url) => {
        const img = new Image();
        img.src = url;
        try {
          await img.decode();
          return { width: img.naturalWidth, height: img.naturalHeight };
        } catch {
          // 单张解码失败时跳过该样本。
          return null;
        }
      }),
    );
    return (
      medianSpineAspect(dims.filter((dim) => dim !== null)) ??
      FALLBACK_SPINE_ASPECT
    );
  }

  async function openCrop(file: File) {
    setReferenceAspect(await measureReferenceAspect());
    setCropFile(file);
  }

  // 编辑器里引用非当前页模板时，按需补拉缩略图。
  function ensureSpineLoaded(id: string) {
    if (id in spineUrls) return;
    libraryImageUrl(id)
      .then((url) =>
        setSpineUrls((previous) =>
          id in previous ? previous : { ...previous, [id]: url },
        ),
      )
      .catch(() =>
        setSpineUrls((previous) =>
          id in previous ? previous : { ...previous, [id]: null },
        ),
      );
  }

  function openCreate() {
    setEditing(undefined);
    setName('');
    setTemplateId(undefined);
    setSpineUpload(undefined);
    setHeightMm(DEFAULT_BOOK_HEIGHT_MM);
    setAnnotationId(undefined);
    setPreferredSlot(undefined);
    setEditorOpen(true);
  }

  function openEdit(book: Book) {
    setEditing(book);
    setName(book.name);
    setTemplateId(book.templateId);
    ensureSpineLoaded(book.templateId);
    setSpineUpload(undefined);
    setHeightMm(book.heightMm);
    setAnnotationId(book.annotationId ?? undefined);
    setPreferredSlot(book.preferredSlot ?? undefined);
    setEditorOpen(true);
  }

  async function save() {
    if (!name.trim() || !(heightMm > 0)) {
      message.warning('请填写书名和取书高度。');
      return;
    }
    if (!editing && !spineFile && !templateId) {
      message.warning('请上传书脊图片，或从机器人书脊库选图。');
      return;
    }
    setSaving(true);
    try {
      let finalTemplateId = templateId;
      if (spineFile) {
        const imported = await importLibraryImage(spineFile, name.trim());
        finalTemplateId = imported.id;
        if (imported.selfCheck && !imported.selfCheck.ok) {
          message.warning(
            `特征不足，建议重拍：${imported.selfCheck.detail ?? '该书脊图可能无法被稳定识别。'}`,
          );
        }
      }
      if (!finalTemplateId) {
        message.warning('请上传书脊图片，或从机器人书脊库选图。');
        return;
      }
      if (duplicate) {
        message.warning(`已存在同名书籍《${duplicate.name}》，请注意区分。`);
      }
      const values = {
        name: name.trim(),
        templateId: finalTemplateId,
        heightMm,
        annotationId: annotationId ?? null,
        preferredSlot: preferredSlot ?? null,
      };
      if (editing) {
        await updateBook(editing.id, values);
        message.success('书籍已更新。');
      } else {
        await createBook(values);
        message.success('书籍已创建。');
      }
      setEditorOpen(false);
      await refresh();
    } catch (error) {
      message.error(errorMessage(error, '保存书籍失败。'));
    } finally {
      setSaving(false);
    }
  }

  async function remove(book: Book) {
    try {
      await deleteBook(book.id);
      message.success(`已删除《${book.name}》。`);
      await refresh();
    } catch (error) {
      message.error(errorMessage(error, '删除书籍失败。'));
    }
  }

  async function runImport() {
    if (!csvFile || !zipFile) {
      message.warning('请分别选择 CSV 清单与书脊图 zip 包。');
      return;
    }
    setImporting(true);
    try {
      const result = await importBooks(csvFile, zipFile);
      setImportResult(result);
      if (result.failed.length === 0) {
        message.success(`批量导入完成：${result.succeeded} 本全部成功。`);
      } else {
        message.warning(
          `批量导入完成：成功 ${result.succeeded}/${result.total} 行，失败 ${result.failed.length} 行（明细见下）。`,
        );
      }
      await refresh();
    } catch (error) {
      message.error(errorMessage(error, '批量导入失败。'));
    } finally {
      setImporting(false);
    }
  }

  async function openPicker() {
    setPickerOpen(true);
    setPickerLoading(true);
    try {
      const { items } = await listLibrary();
      setLibraryItems(items);
      const entries = await Promise.all(
        items.map(
          async (item) =>
            [item.id, await libraryImageUrl(item.id).catch(() => '')] as const,
        ),
      );
      setPickerUrls((previous) => {
        revokeUrls(previous);
        return Object.fromEntries(entries.filter(([, url]) => Boolean(url)));
      });
    } catch (error) {
      message.error(errorMessage(error, '无法读取机器人书脊库。'));
    } finally {
      setPickerLoading(false);
    }
  }

  async function runSelfCheck() {
    setChecking(true);
    try {
      const [verify, library] = await Promise.all([
        verifyLibrary(),
        listLibrary(),
      ]);
      const verifyMap = new Map(verify.map((item) => [item.id, item]));
      const ids = new Set(library.items.map((item) => item.id));
      setLibraryIds(ids);
      const rows: CheckRow[] = books.map((book) => {
        if (!ids.has(book.templateId)) {
          return {
            book,
            status: '模板缺失',
            detail: '书脊库中无此图，请重新补录书脊图。',
          };
        }
        const verdict = verifyMap.get(book.templateId);
        if (verdict && !verdict.ok) {
          return {
            book,
            status: '特征不足',
            detail: verdict.detail ?? '特征不足，建议重拍。',
          };
        }
        return { book, status: 'ok' };
      });
      setCheckRows(rows);
      setCheckOpen(true);
      const problems = rows.filter((row) => row.status !== 'ok').length;
      if (problems === 0) message.success('全部书籍的书脊图自检通过。');
    } catch (error) {
      message.error(errorMessage(error, '识别自检失败。'));
    } finally {
      setChecking(false);
    }
  }

  function spineThumb(id: string, height = 64) {
    if (!(id in spineUrls)) {
      return <Typography.Text type="secondary">加载中…</Typography.Text>;
    }
    const url = spineUrls[id];
    if (!url) return <Tag>未入库</Tag>;
    return (
      <img
        src={url}
        alt={id}
        style={{ height, borderRadius: 4, display: 'block' }}
      />
    );
  }

  const problemRows = checkRows.filter((row) => row.status !== 'ok');

  return (
    <PageContainer
      title="书籍管理"
      content="登记书名、书脊图与取书高度（升降台目标高度），并关联取书点位与常用槽位，供快速取书 / 快速还书 / 多轮取还书按书名选择。书脊图可来自书脊工作台分割、机器人书脊库或本地上传，也可用 CSV+zip 批量导入。"
    >
      <Card
        size="small"
        title="书籍列表"
        extra={
          <Space wrap>
            <Input
              allowClear
              prefix={<SearchOutlined />}
              placeholder="搜索书名"
              style={{ width: 200 }}
              value={keyword}
              onChange={(event) => {
                setKeyword(event.target.value);
                setPage(1);
              }}
            />
            <Button
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => void refresh()}
            />
            <Tooltip title="对每本书的书脊模板调机器人识别自检，列出不可识别的书">
              <Button
                icon={<CheckCircleOutlined />}
                loading={checking}
                onClick={() => void runSelfCheck()}
              >
                识别自检
              </Button>
            </Tooltip>
            <Button
              icon={<FileAddOutlined />}
              onClick={() => setImportOpen(true)}
            >
              批量导入
            </Button>
            <Button
              icon={<DownloadOutlined />}
              onClick={() => {
                exportLibraryBackup().catch((error) =>
                  message.error(errorMessage(error, '导出备份失败。')),
                );
              }}
            >
              导出备份
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新增书籍
            </Button>
          </Space>
        }
      >
        <Table<Book>
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={filtered}
          pagination={{
            current: page,
            pageSize,
            total: filtered.length,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (total) => `共 ${total} 本`,
          }}
          onChange={(pagination) => {
            setPage(pagination.current ?? 1);
            setPageSize(pagination.pageSize ?? 20);
          }}
          locale={{ emptyText: '还没有登记书籍' }}
          columns={[
            {
              title: '书名',
              dataIndex: 'name',
              render: (value: string, book) => (
                <Space size={6}>
                  {value}
                  {missingTemplates.has(book.templateId) ? (
                    <Tooltip title="书脊库中无此图，创建取书任务前请先补录书脊图">
                      <Tag color="error">模板缺失</Tag>
                    </Tooltip>
                  ) : null}
                </Space>
              ),
            },
            {
              title: '书脊图',
              dataIndex: 'templateId',
              width: 120,
              render: (id: string) => spineThumb(id),
            },
            {
              title: (
                <Space size={4}>
                  取书高度（升降台 mm）
                  <Tooltip title={HEIGHT_TOOLTIP}>
                    <QuestionCircleOutlined />
                  </Tooltip>
                </Space>
              ),
              dataIndex: 'heightMm',
              width: 190,
            },
            {
              title: '取书点',
              dataIndex: 'annotationId',
              width: 160,
              render: (id: string | null) =>
                id
                  ? (annotations.find((item) => item.id === id)?.label ?? id)
                  : '—',
            },
            {
              title: '常用槽位',
              dataIndex: 'preferredSlot',
              width: 90,
              render: (slot: number | null) => (slot ? `槽位 ${slot}` : '—'),
            },
            {
              title: '更新时间',
              dataIndex: 'updatedAt',
              width: 180,
              render: (value: string) => formatBookTime(value),
            },
            {
              title: '操作',
              width: 140,
              render: (_, book) => (
                <Space>
                  <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => openEdit(book)}
                  />
                  <Popconfirm
                    title={`删除《${book.name}》？`}
                    description="删除后多轮取还书将无法再选择该书。"
                    okText="确认删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void remove(book)}
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                    />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Modal
        destroyOnHidden
        open={editorOpen}
        title={editing ? '编辑书籍' : '新增书籍'}
        okText={editing ? '保存' : '创建'}
        cancelText="取消"
        confirmLoading={saving}
        onCancel={() => setEditorOpen(false)}
        onOk={() => void save()}
      >
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <Typography.Text>书名</Typography.Text>
            <Input
              value={name}
              maxLength={80}
              placeholder="例如：深入理解计算机系统"
              onChange={(event) => setName(event.target.value)}
            />
            {duplicate ? (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>
                已存在同名书籍《{duplicate.name}》，可继续保存，请注意区分。
              </Typography.Text>
            ) : null}
          </div>
          <div>
            <Typography.Text>书脊图</Typography.Text>
            <br />
            <Space wrap>
              <Upload
                accept={SPINE_IMAGE_TYPES.join(',')}
                showUploadList={false}
                beforeUpload={(file) => {
                  if (!SPINE_IMAGE_TYPES.includes(file.type)) {
                    message.error('书脊图片只支持 JPG / PNG。');
                    return Upload.LIST_IGNORE;
                  }
                  if (file.size > SPINE_IMAGE_MAX_BYTES) {
                    message.error('书脊图片不能超过 10MB。');
                    return Upload.LIST_IGNORE;
                  }
                  void openCrop(file);
                  return false;
                }}
              >
                <Button icon={<UploadOutlined />}>
                  {spineFile ? spineFile.name : '本地上传'}
                </Button>
              </Upload>
              <Button
                icon={<PictureOutlined />}
                onClick={() => void openPicker()}
              >
                从机器人书脊库选图
              </Button>
            </Space>
            <br />
            {editing && !spineFile && templateId === editing.templateId ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                不重新选择则保持当前书脊图不变。
              </Typography.Text>
            ) : null}
          </div>
          <div>
            <Typography.Text>
              取书高度（升降台 mm）{' '}
              <Tooltip title={HEIGHT_TOOLTIP}>
                <QuestionCircleOutlined />
              </Tooltip>
            </Typography.Text>
            <InputNumber
              style={{ width: '100%' }}
              min={10}
              max={1700}
              precision={0}
              value={heightMm}
              onChange={(value) =>
                setHeightMm(Number(value ?? DEFAULT_BOOK_HEIGHT_MM))
              }
            />
          </div>
          <div>
            <Typography.Text>取书点（地图标注，可空）</Typography.Text>
            <br />
            <AnnotationTargetSelect
              annotations={annotations}
              value={annotationId}
              onChange={setAnnotationId}
            />
          </div>
          <div>
            <Typography.Text>常用槽位（可空）</Typography.Text>
            <br />
            <Select
              allowClear
              style={{ width: 160 }}
              placeholder="不指定"
              value={preferredSlot}
              onChange={(value) => setPreferredSlot(value)}
              options={BOOK_STAND_SLOTS.map((slotNo) => ({
                value: slotNo,
                label: `槽位 ${slotNo}`,
              }))}
            />
          </div>
          {spineFileUrl ? (
            <Space align="start">
              <Typography.Text type="secondary">新图预览：</Typography.Text>
              <img
                src={spineFileUrl}
                alt="待上传书脊图"
                style={{ height: 96, borderRadius: 4 }}
              />
            </Space>
          ) : templateId ? (
            <Space align="start">
              <Typography.Text type="secondary">当前书脊图：</Typography.Text>
              {spineThumb(templateId, 96)}
            </Space>
          ) : null}
        </Space>
      </Modal>
      <SpineCropModal
        open={Boolean(cropFile)}
        file={cropFile}
        referenceAspect={referenceAspect}
        onConfirm={(cropped) => {
          setSpineUpload(cropped);
          setCropFile(undefined);
        }}
        onUseOriginal={() => {
          if (cropFile) setSpineUpload(cropFile);
          setCropFile(undefined);
        }}
        onCancel={() => setCropFile(undefined)}
      />
      <Modal
        destroyOnHidden
        open={importOpen}
        title="批量导入书籍"
        okText="开始导入"
        cancelText="关闭"
        confirmLoading={importing}
        onCancel={() => setImportOpen(false)}
        onOk={() => void runImport()}
      >
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            showIcon
            type="info"
            title="CSV 每行一本：书名,书脊图文件名,高度mm,点位标注名,槽位（后两列可空）；zip 打包 CSV 引用的全部书脊图（JPG/PNG，单张 ≤10MB，zip ≤20MB）。点位标注名需与地图标注完全一致。"
          />
          <Space wrap>
            <Upload
              accept=".csv"
              showUploadList={false}
              beforeUpload={(file) => {
                setCsvFile(file);
                setImportResult(undefined);
                return false;
              }}
            >
              <Button icon={<UploadOutlined />}>
                {csvFile ? csvFile.name : '选择 CSV'}
              </Button>
            </Upload>
            <Upload
              accept=".zip"
              showUploadList={false}
              beforeUpload={(file) => {
                setZipFile(file);
                setImportResult(undefined);
                return false;
              }}
            >
              <Button icon={<UploadOutlined />}>
                {zipFile ? zipFile.name : '选择书脊图 zip'}
              </Button>
            </Upload>
          </Space>
          {importResult ? (
            <>
              <Alert
                showIcon
                type={importResult.failed.length ? 'warning' : 'success'}
                title={`共 ${importResult.total} 行：成功 ${importResult.succeeded} 行，失败 ${importResult.failed.length} 行。`}
              />
              {importResult.failed.length ? (
                <Table
                  rowKey="row"
                  size="small"
                  dataSource={importResult.failed}
                  pagination={false}
                  columns={[
                    { title: 'CSV 行号', dataIndex: 'row', width: 90 },
                    { title: '失败原因', dataIndex: 'reason' },
                  ]}
                />
              ) : null}
            </>
          ) : null}
        </Space>
      </Modal>
      <Modal
        destroyOnHidden
        open={pickerOpen}
        title="从机器人书脊库选图"
        footer={null}
        width={720}
        onCancel={() => setPickerOpen(false)}
      >
        {libraryItems.length === 0 && !pickerLoading ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="机器人书脊库为空，请先在书脊工作台分割或本地上传"
          />
        ) : (
          <List
            loading={pickerLoading}
            grid={{ gutter: 12, xs: 3, sm: 4, md: 6 }}
            dataSource={libraryItems}
            renderItem={(item) => (
              <List.Item>
                <Card
                  size="small"
                  hoverable
                  onClick={() => {
                    setTemplateId(item.id);
                    ensureSpineLoaded(item.id);
                    setSpineUpload(undefined);
                    setPickerOpen(false);
                    if (!name.trim() && item.name) setName(item.name);
                  }}
                >
                  <div style={{ textAlign: 'center' }}>
                    {pickerUrls[item.id] ? (
                      <img
                        src={pickerUrls[item.id]}
                        alt={item.name ?? item.id}
                        style={{ height: 96, borderRadius: 4 }}
                      />
                    ) : (
                      <div style={{ height: 96 }}>无预览</div>
                    )}
                    <Typography.Text
                      ellipsis
                      style={{ display: 'block', fontSize: 12 }}
                    >
                      {item.name || item.id}
                    </Typography.Text>
                  </div>
                </Card>
              </List.Item>
            )}
          />
        )}
      </Modal>
      <Modal
        destroyOnHidden
        open={checkOpen}
        title="识别自检结果"
        footer={null}
        width={640}
        onCancel={() => setCheckOpen(false)}
      >
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            showIcon
            type={problemRows.length ? 'warning' : 'success'}
            title={
              problemRows.length
                ? `${problemRows.length} 本书无法稳定识别，请按下表补录或重拍。`
                : '全部书籍的书脊图均可识别。'
            }
          />
          {problemRows.length ? (
            <Table<CheckRow>
              rowKey={(row) => row.book.id}
              size="small"
              dataSource={problemRows}
              pagination={false}
              columns={[
                {
                  title: '书名',
                  render: (_, row) => row.book.name,
                },
                {
                  title: '问题',
                  dataIndex: 'status',
                  width: 110,
                  render: (status: CheckRow['status']) => (
                    <Tag color={status === '模板缺失' ? 'error' : 'warning'}>
                      {status}
                    </Tag>
                  ),
                },
                { title: '说明', dataIndex: 'detail' },
              ]}
            />
          ) : null}
        </Space>
      </Modal>
    </PageContainer>
  );
}
