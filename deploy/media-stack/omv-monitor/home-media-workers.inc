<?php
namespace Engined\Rpc;
class HomeMediaWorkers extends \OMV\Rpc\ServiceAbstract
{
    public function getName() { return 'HomeMediaWorkers'; }
    public function initialize() {
        $this->registerMethod('getWorkers');
        $this->registerMethod('getCounts');
    }
    private function readSection($section, $params, $context) {
        $this->validateMethodContext($context, ['role' => OMV_ROLE_ADMINISTRATOR]);
        // The command and section are fixed internally, never caller supplied.
        $status = $this->exec('/usr/bin/timeout 25 /usr/bin/python3 /opt/media-stack/omv-monitor/monitor.py ' . $section, $output);
        if ($status !== 0) throw new \OMV\Exception('Worker monitor could not read the current status.');
        $rows = json_decode(implode("\n", $output), true, 512, JSON_THROW_ON_ERROR);
        return $this->applyFilter($rows, max(0, (int)($params['start'] ?? 0)), (int)($params['limit'] ?? -1), $params['sortfield'] ?? null, $params['sortdir'] ?? null);
    }
    public function getWorkers($params, $context) { return $this->readSection('workers', $params, $context); }
    public function getCounts($params, $context) { return $this->readSection('counts', $params, $context); }
}
