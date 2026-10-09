import { useState } from 'react'
import { App as AntApp, ConfigProvider, Layout, Menu, Space, Tag, Typography } from 'antd'
import { StudioProvider, useStudio } from './StudioContext'
import ConfigPage from './pages/ConfigPage'
import DatasetPage from './pages/DatasetPage'
import GeneratePage from './pages/GeneratePage'
import OrmPage from './pages/OrmPage'
import SearchPage from './pages/SearchPage'
import ServePage from './pages/ServePage'
import TricksPage from './pages/TricksPage'

const MENU = [
  { key: 'generate', label: 'Text2SQL 使用' },
  { key: 'config', label: '配置' },
  { key: 'orm', label: 'ORM 配置' },
  { key: 'dataset', label: '测评数据集制作' },
  { key: 'search', label: '自动寻优' },
  { key: 'serve', label: '服务模式' },
  { key: 'tricks', label: '完整 Trick 地图' },
]

function Shell() {
  const [page, setPage] = useState('generate')
  const { current, strategy } = useStudio()

  const content = {
    generate: <GeneratePage />,
    config: <ConfigPage />,
    orm: <OrmPage />,
    dataset: <DatasetPage />,
    search: <SearchPage />,
    serve: <ServePage />,
    tricks: <TricksPage />,
  }[page]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Sider theme="light" width={208}>
        <div className="brand">
          <span className="brand-dot" />
          SqlStage
        </div>
        <Menu mode="inline" selectedKeys={[page]} onClick={(event) => setPage(event.key)}
          style={{ borderInlineEnd: 'none', paddingTop: 8 }} items={MENU} />
      </Layout.Sider>
      <Layout>
        <Layout.Header style={{ background: '#fff', display: 'flex', alignItems: 'center', gap: 16 }}>
          <Space size={8}>
            <Typography.Text type="secondary">当前连接</Typography.Text>
            {current ? <Tag color="blue">{current.name} · {current.type}</Tag> : <Tag>未选择</Tag>}
          </Space>
          <Space size={8}>
            <Typography.Text type="secondary">策略方案</Typography.Text>
            <Tag color="geekblue">{strategy.name}</Tag>
          </Space>
        </Layout.Header>
        <Layout.Content style={{ padding: '24px 28px 48px' }}>
          <div style={{ maxWidth: 1200, margin: '0 auto' }}>{content}</div>
        </Layout.Content>
      </Layout>
    </Layout>
  )
}

export default function App() {
  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: '#2563eb',
          colorBgLayout: '#f4f6f9',
          borderRadius: 8,
          fontSize: 14,
        },
        components: {
          Layout: { headerHeight: 56, headerPadding: '0 24px' },
          Card: { headerFontSize: 15, paddingLG: 20 },
          Table: { headerBg: '#f7f8fa', cellPaddingBlockSM: 8 },
        },
      }}
    >
      <AntApp>
        <StudioProvider>
          <Shell />
        </StudioProvider>
      </AntApp>
    </ConfigProvider>
  )
}
